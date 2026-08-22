"""Rotation across a pool of mailboxes: spread, caps, pinning, deferral.

The behaviour under test is the whole point of connecting more than one
mailbox: volume goes up by spreading it, and running out of room is a wait
rather than a failure. The deferral tests are the important ones — getting that
wrong loses sends permanently rather than delaying them, because the
outreach.send idempotency key is permanent.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from juicebox.jobs import KIND_OUTREACH_SEND, enqueue
from juicebox.models import (
    Enrollment,
    Job,
    MailboxConnection,
    Message,
    SequenceStep,
    utcnow,
)
from juicebox.outreach.rotation import (
    eligible_mailboxes,
    pick_mailbox,
    pool_capacity,
    sent_in_window,
)
from juicebox.outreach.sender import SendResult, resolve_sender, set_sender_factory
from juicebox.outreach.sequencer import enroll_candidates, process_enrollment, run_due


class PoolSender:
    """Records which mailbox each send went out from."""

    name = "graph"

    def __init__(self, connection, log: list[str]):
        self.connection = connection
        self.log = log

    def send(self, email):
        self.log.append(self.connection.address)
        native = f"native-{len(self.log)}"
        return SendResult(
            ok=True,
            provider_message_id=f"<{native}@test>",
            provider_native_id=native,
            internet_message_id=f"<{native}@test>",
            conversation_id=f"conv-{len(self.log)}",
        )


@pytest.fixture
def pool(session, account, outreach_fixture):
    """Three connected mailboxes, a rotating sequence, and a send log."""
    connections = []
    for name in ("alpha", "bravo", "charlie"):
        connection = MailboxConnection(
            org_id=account["org_id"],
            address=f"{name}@goforay.io",
            daily_send_cap=2,
        )
        session.add(connection)
        connections.append(connection)
    session.flush()

    log: list[str] = []
    set_sender_factory("microsoft", lambda db, conn: PoolSender(conn, log))

    sequence = outreach_fixture["sequence"]
    sequence.sender_mode = "rotate"
    sequence.mailbox_id = None
    session.commit()
    return {
        "connections": connections,
        "sequence": sequence,
        "log": log,
        "candidates": outreach_fixture["candidates"],
    }


class TestEligibility:
    def test_a_zero_cap_means_stop_not_unlimited(self, session, pool):
        pool["connections"][0].daily_send_cap = 0
        session.flush()
        addresses = {c.address for c in eligible_mailboxes(session, org_id=pool["sequence"].org_id)}
        assert "alpha@goforay.io" not in addresses
        assert len(addresses) == 2

    def test_switching_sending_off_removes_it_from_the_pool(self, session, pool):
        pool["connections"][1].send_enabled = False
        session.flush()
        addresses = {c.address for c in eligible_mailboxes(session, org_id=pool["sequence"].org_id)}
        assert "bravo@goforay.io" not in addresses

    def test_revoked_mailboxes_are_not_eligible(self, session, pool):
        pool["connections"][2].status = "revoked"
        session.flush()
        addresses = {c.address for c in eligible_mailboxes(session, org_id=pool["sequence"].org_id)}
        assert "charlie@goforay.io" not in addresses

    def test_pool_capacity_is_the_sum_of_the_caps(self, session, pool):
        assert pool_capacity(session, org_id=pool["sequence"].org_id) == 6

    def test_only_sent_messages_consume_a_slot(self, session, pool, outreach_fixture):
        """A queued row has not spent reputation and a failed one never left."""
        connection = pool["connections"][0]
        now = utcnow()
        for status in ("queued", "failed", "sent"):
            session.add(
                Message(
                    org_id=connection.org_id,
                    candidate_id=outreach_fixture["candidates"][0].id,
                    direction="outbound",
                    status=status,
                    mailbox_id=connection.id,
                    sent_at=now,
                )
            )
        session.flush()
        assert sent_in_window(session, mailbox_id=connection.id, now=now) == 1

    def test_the_window_rolls_rather_than_resetting_at_midnight(
        self, session, pool, outreach_fixture
    ):
        connection = pool["connections"][0]
        now = utcnow()
        session.add(
            Message(
                org_id=connection.org_id,
                candidate_id=outreach_fixture["candidates"][0].id,
                direction="outbound",
                status="sent",
                mailbox_id=connection.id,
                sent_at=now - timedelta(hours=25),
            )
        )
        session.flush()
        assert sent_in_window(session, mailbox_id=connection.id, now=now) == 0


class TestSpread:
    def test_sends_spread_evenly_across_the_pool(self, session, pool):
        """Three candidates over three mailboxes: one each, not three on one."""
        for connection in pool["connections"]:
            connection.daily_send_cap = 1
        session.flush()
        candidate_ids = [c.id for c in pool["candidates"]]
        assert len(candidate_ids) == 3
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=candidate_ids)
        session.commit()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()

        assert sorted(pool["log"]) == [
            "alpha@goforay.io",
            "bravo@goforay.io",
            "charlie@goforay.io",
        ]
        # Every cap is now spent, so the pool reports no room rather than
        # quietly overshooting.
        assert pick_mailbox(session, org_id=pool["sequence"].org_id, now=utcnow()) is None

    def test_the_org_cap_lifts_to_the_pool_size(self, session, pool, monkeypatch):
        """A 200-send org cap must not silently defer a pool that can carry
        more; the per-mailbox caps are the real deliverability control."""
        monkeypatch.setenv("OUTREACH_DAILY_CAP", "1")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        for connection in pool["connections"]:
            connection.daily_send_cap = 3
        session.flush()

        candidate_ids = [c.id for c in pool["candidates"]]
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=candidate_ids)
        session.commit()
        summary = run_due(session, org_id=pool["sequence"].org_id)
        session.commit()
        # An org cap of 1 would have allowed one send; the pool carries nine.
        assert summary.sent == 3


class TestPinning:
    def test_the_first_send_pins_the_enrollment(self, session, pool):
        candidate = pool["candidates"][0]
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()

        enrollment = session.query(Enrollment).one()
        assert enrollment.mailbox_id is not None
        chosen = session.get(MailboxConnection, enrollment.mailbox_id)
        assert chosen.address == pool["log"][0]

    def test_a_pinned_enrollment_keeps_its_mailbox_for_later_steps(self, session, pool):
        """Rotation must not move a live conversation: the candidate's client
        threads on the address, so a second mailbox starts a second thread."""
        candidate = pool["candidates"][0]
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()

        enrollment = session.query(Enrollment).one()
        pinned = enrollment.mailbox_id
        # Make the second step due, and load the other mailboxes up so plain
        # rotation would definitely pick somebody else.
        enrollment.next_send_at = utcnow() - timedelta(minutes=1)
        session.flush()

        resolved = resolve_sender(session, pool["sequence"], enrollment)
        assert resolved.connection.id == pinned

        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()
        assert len(pool["log"]) == 2
        assert pool["log"][0] == pool["log"][1]

    def test_a_revoked_pin_fails_loudly_instead_of_moving_address(self, session, pool):
        candidate = pool["candidates"][0]
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()

        enrollment = session.query(Enrollment).one()
        session.get(MailboxConnection, enrollment.mailbox_id).status = "revoked"
        enrollment.next_send_at = utcnow() - timedelta(minutes=1)
        session.flush()

        resolved = resolve_sender(session, pool["sequence"], enrollment)
        assert not resolved.deferred
        result = resolved.sender.send(None)
        assert not result.ok and "reconnect" in result.error

    def test_fixed_mode_overrides_the_pool(self, session, pool):
        pinned = pool["connections"][2]
        pool["sequence"].sender_mode = "fixed"
        pool["sequence"].mailbox_id = pinned.id
        session.flush()

        candidate_ids = [c.id for c in pool["candidates"]][:2]
        pinned.daily_send_cap = 5
        session.flush()
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=candidate_ids)
        session.commit()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()
        assert set(pool["log"]) == {pinned.address}


class TestDeferral:
    def test_a_full_pool_defers_rather_than_failing(self, session, pool):
        for connection in pool["connections"]:
            connection.daily_send_cap = 0
        session.flush()

        candidate = pool["candidates"][0]
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        enrollment = session.query(Enrollment).one()
        before = enrollment.next_send_at

        status, message = process_enrollment(session, enrollment, now=utcnow())
        assert status == "deferred"
        assert message is None
        # Nothing about the enrollment moved: the step is still due, still on
        # step 0, and will be retried as-is.
        assert enrollment.current_step == 0
        assert enrollment.next_send_at == before
        assert enrollment.status == "active"
        assert session.query(Message).count() == 0

    def test_a_deferred_send_job_stays_queued(self, session, pool):
        """Not failed (which would park the permanent idempotency key) and not
        succeeded (which would lose the step forever)."""
        from juicebox.worker import process_job

        for connection in pool["connections"]:
            connection.daily_send_cap = 0
        session.flush()

        candidate = pool["candidates"][0]
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        enrollment = session.query(Enrollment).one()
        step = sorted(pool["sequence"].steps, key=lambda s: s.step_order)[0]
        job = enqueue(
            session,
            org_id=pool["sequence"].org_id,
            kind=KIND_OUTREACH_SEND,
            idempotency_key=f"{KIND_OUTREACH_SEND}:{enrollment.id}:{step.id}",
            payload={
                "org_id": pool["sequence"].org_id,
                "enrollment_id": enrollment.id,
                "step_id": step.id,
            },
        )
        process_job(session, job)
        session.commit()

        assert job.status == "queued"
        assert job.attempts == 0
        assert session.query(Message).count() == 0

    def test_capacity_freeing_up_lets_the_deferred_step_send(self, session, pool):
        from juicebox.worker import process_job

        for connection in pool["connections"]:
            connection.daily_send_cap = 0
        session.flush()

        candidate = pool["candidates"][0]
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        enrollment = session.query(Enrollment).one()
        step = sorted(pool["sequence"].steps, key=lambda s: s.step_order)[0]
        job = enqueue(
            session,
            org_id=pool["sequence"].org_id,
            kind=KIND_OUTREACH_SEND,
            idempotency_key=f"{KIND_OUTREACH_SEND}:{enrollment.id}:{step.id}",
            payload={
                "org_id": pool["sequence"].org_id,
                "enrollment_id": enrollment.id,
                "step_id": step.id,
            },
        )
        process_job(session, job)
        session.commit()
        assert job.status == "queued" and not pool["log"]

        pool["connections"][0].daily_send_cap = 5
        session.flush()
        job.run_at = utcnow() - timedelta(seconds=1)
        process_job(session, job)
        session.commit()

        assert job.status == "succeeded"
        assert pool["log"] == ["alpha@goforay.io"]
        assert session.query(Job).filter_by(id=job.id).one().status == "succeeded"


class TestThreadingHeaders:
    def test_a_follow_up_references_the_previous_send(self, session, pool):
        """Providers that can express In-Reply-To keep the follow-up on the
        same thread in the candidate's client."""
        captured: list = []

        class Capturing(PoolSender):
            def send(self, email):
                captured.append(email)
                return super().send(email)

        set_sender_factory("microsoft", lambda db, conn: Capturing(conn, pool["log"]))

        candidate = pool["candidates"][0]
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()

        enrollment = session.query(Enrollment).one()
        enrollment.next_send_at = utcnow() - timedelta(minutes=1)
        session.flush()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()

        assert len(captured) == 2
        # The first send has nothing to thread off; the second points at it.
        assert captured[0].in_reply_to == ""
        assert captured[1].in_reply_to == "<native-1@test>"
        assert captured[1].references == "<native-1@test>"
        # And the provider's own id for the parent, which is the only thing
        # Graph can thread a send on.
        assert captured[0].thread_native_id == ""
        assert captured[1].thread_native_id == "native-1"

    def test_the_references_chain_keeps_every_earlier_message(self, session, pool):
        """References is a chain: a third step that named only its immediate
        parent would drop out of the thread in any client that missed step 2."""
        captured: list = []

        class Capturing(PoolSender):
            def send(self, email):
                captured.append(email)
                return super().send(email)

        set_sender_factory("microsoft", lambda db, conn: Capturing(conn, pool["log"]))
        for connection in pool["connections"]:
            connection.daily_send_cap = 5
        session.add(
            SequenceStep(
                sequence_id=pool["sequence"].id,
                step_order=2,
                delay_days=3,
                subject_template="Re: {{ role_title }}",
                body_template="Last note, {{ first_name }}.",
            )
        )
        session.commit()
        # The sequence already has its steps loaded; without this the run would
        # still see a two-step sequence and complete after step 2.
        session.refresh(pool["sequence"])

        candidate = pool["candidates"][0]
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        for _ in range(3):
            enrollment = session.query(Enrollment).one()
            enrollment.next_send_at = utcnow() - timedelta(minutes=1)
            session.flush()
            run_due(session, org_id=pool["sequence"].org_id)
            session.commit()

        assert len(captured) == 3
        assert captured[2].in_reply_to == "<native-2@test>"
        assert captured[2].references == "<native-1@test> <native-2@test>"

    def test_a_follow_up_keeps_the_threads_subject(self, session, pool):
        """A changed subject line splits the conversation in Gmail and Outlook
        even when the headers are right, so a step that renders the same
        subject as the first email sends as a reply to it.

        The same subject on every step is how the production sequence is
        written, and how anyone bumping their own email writes it.
        """
        candidate = pool["candidates"][0]
        for connection in pool["connections"]:
            connection.daily_send_cap = 5
        steps = sorted(pool["sequence"].steps, key=lambda s: s.step_order)
        steps[1].subject_template = steps[0].subject_template
        session.flush()
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()

        enrollment = session.query(Enrollment).one()
        enrollment.next_send_at = utcnow() - timedelta(minutes=1)
        session.flush()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()

        first, second = (
            session.query(Message)
            .filter_by(direction="outbound", status="sent")
            .order_by(Message.sent_at)
            .all()
        )
        assert not first.subject.lower().startswith("re:")
        assert second.subject == f"Re: {first.subject}"

    def _run_two_steps(self, session, pool, follow_up_subject):
        candidate = pool["candidates"][0]
        for connection in pool["connections"]:
            connection.daily_send_cap = 5
        steps = sorted(pool["sequence"].steps, key=lambda s: s.step_order)
        steps[1].subject_template = follow_up_subject
        session.flush()

        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()
        enrollment = session.query(Enrollment).one()
        enrollment.next_send_at = utcnow() - timedelta(minutes=1)
        session.flush()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()

        return (
            session.query(Message)
            .filter_by(direction="outbound", status="sent")
            .order_by(Message.sent_at)
            .all()
        )

    def test_a_follow_up_ignores_a_subject_of_its_own(self, session, pool):
        """A different subject splits the conversation in Gmail and Outlook
        however correct the headers are, so a follow-up does not get to have
        one. The step editor stopped offering the box; this covers the
        sequences written before it did, whose step 2 still holds text.
        """
        first, second = self._run_two_steps(session, pool, "A different question")

        assert second.subject == f"Re: {first.subject}"

    def test_a_follow_up_with_no_subject_inherits_rather_than_going_out_blank(
        self, session, pool
    ):
        """What the editor now saves for a follow-up. Without inheritance this
        would fall through to the generic "Quick note for ..." subject, which
        is exactly the changed line that breaks the thread."""
        first, second = self._run_two_steps(session, pool, "")

        assert second.subject == f"Re: {first.subject}"
        assert "Quick note" not in second.subject

    def test_a_third_step_says_re_once_not_twice(self, session, pool):
        """Every step inherits the ROOT subject, so the "Re:" never stacks."""
        candidate = pool["candidates"][0]
        for connection in pool["connections"]:
            connection.daily_send_cap = 5
        session.add(
            SequenceStep(
                sequence_id=pool["sequence"].id,
                step_order=2,
                delay_days=3,
                subject_template="",
                body_template="Last note, {{ first_name }}.",
            )
        )
        session.commit()
        session.refresh(pool["sequence"])

        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        for _ in range(3):
            enrollment = session.query(Enrollment).one()
            enrollment.next_send_at = utcnow() - timedelta(minutes=1)
            session.flush()
            run_due(session, org_id=pool["sequence"].org_id)
            session.commit()

        first, second, third = (
            session.query(Message)
            .filter_by(direction="outbound", status="sent")
            .order_by(Message.sent_at)
            .all()
        )
        assert second.subject == f"Re: {first.subject}"
        assert third.subject == f"Re: {first.subject}"

