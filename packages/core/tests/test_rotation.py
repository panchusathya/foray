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
    frees_at,
    pick_mailbox,
    sent_in_window,
)
from juicebox.outreach.sender import SendResult, resolve_sender, set_sender_factory
from juicebox.outreach.sequencer import (
    LANE_SLACK,
    dispatch_line,
    enroll_candidates,
    process_enrollment,
    run_due,
)


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


class TestFreesAt:
    def _sent(self, session, connection, candidate, at):
        session.add(
            Message(
                org_id=connection.org_id,
                candidate_id=candidate.id,
                direction="outbound",
                status="sent",
                mailbox_id=connection.id,
                sent_at=at,
            )
        )
        session.flush()

    def test_a_mailbox_with_room_frees_now(self, session, pool):
        now = utcnow()
        assert frees_at(session, connection=pool["connections"][0], now=now) == now

    def test_a_full_mailbox_frees_when_its_oldest_send_leaves_the_window(
        self, session, pool, outreach_fixture
    ):
        connection = pool["connections"][0]  # cap 2
        candidate = outreach_fixture["candidates"][0]
        now = utcnow()
        oldest = now - timedelta(hours=20)
        self._sent(session, connection, candidate, oldest)
        self._sent(session, connection, candidate, now - timedelta(hours=1))

        freed = frees_at(session, connection=connection, now=now)

        assert freed.replace(tzinfo=None) == (oldest + timedelta(hours=24)).replace(tzinfo=None)

    def test_a_deferred_send_knows_when_to_come_back(self, session, pool, outreach_fixture):
        candidate = outreach_fixture["candidates"][0]
        now = utcnow()
        for connection in pool["connections"]:
            self._sent(session, connection, candidate, now - timedelta(hours=10))
            self._sent(session, connection, candidate, now - timedelta(hours=2))
        pool["connections"][1].daily_send_cap = 2
        session.flush()

        resolved = resolve_sender(session, pool["sequence"], now=now)

        assert resolved.deferred
        assert resolved.retry_at.replace(tzinfo=None) == (now + timedelta(hours=14)).replace(
            tzinfo=None
        )


class TestTheLineKnowsWhichPoolsAreFull:
    def _fill(self, session, connection, candidate, now):
        for hours in (3, 2):
            session.add(
                Message(
                    org_id=connection.org_id,
                    candidate_id=candidate.id,
                    direction="outbound",
                    status="sent",
                    mailbox_id=connection.id,
                    sent_at=now - timedelta(hours=hours),
                )
            )
        session.flush()

    def _line(self, session, pool, now):
        sequence = pool["sequence"]
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        rows = enroll_candidates(
            session, sequence=sequence, candidate_ids=[c.id for c in pool["candidates"]]
        )
        for rank, row in enumerate(rows, start=1):
            row.next_send_at = now
            job = enqueue(
                session,
                org_id=sequence.org_id,
                kind=KIND_OUTREACH_SEND,
                idempotency_key=f"{KIND_OUTREACH_SEND}:{row.id}:{step.id}",
                payload={"org_id": sequence.org_id, "enrollment_id": row.id, "step_id": step.id},
                run_at=now,
            )
            job.rank = float(rank)
        session.commit()
        return rows

    def test_a_full_pool_blocks_its_sends_without_trying_them(self, session, pool):
        now = utcnow()
        for connection in pool["connections"]:
            self._fill(session, connection, pool["candidates"][0], now)
        rows = self._line(session, pool, now)

        counts = dispatch_line(session, pool["sequence"].org_id, now)

        assert counts["blocked"] == len(rows) and counts["awake"] == 0
        assert pool["log"] == []

    def test_a_follow_up_on_a_full_mailbox_does_not_hold_the_line(self, session, pool):
        """The 2026-10-01 stall: sends waiting on full mailboxes sat at the
        front while first emails a free mailbox could carry waited behind."""
        from juicebox.worker import process_batch

        now = utcnow()
        alpha = pool["connections"][0]
        self._fill(session, alpha, pool["candidates"][0], now)
        first, second, *_ = self._line(session, pool, now)
        first.mailbox_id = alpha.id  # pinned to the full one, ranked first
        session.commit()

        counts = dispatch_line(session, pool["sequence"].org_id, now)
        session.commit()
        process_batch(session, now=now)

        assert counts["blocked"] == 1
        assert pool["log"] and "alpha@goforay.io" not in pool["log"]
        session.refresh(second)
        assert second.current_step == 1

    def test_raising_a_cap_wakes_them_on_the_next_tick(self, session, pool):
        now = utcnow()
        for connection in pool["connections"]:
            self._fill(session, connection, pool["candidates"][0], now)
        rows = self._line(session, pool, now)
        dispatch_line(session, pool["sequence"].org_id, now)

        pool["connections"][0].daily_send_cap = 3
        session.flush()
        counts = dispatch_line(session, pool["sequence"].org_id, now)

        assert counts["blocked"] == 0
        # One mailbox (alpha) has room: its lane keeps one send ready plus a
        # spare.
        assert counts["awake"] == min(len(rows), 1 + LANE_SLACK)


class TestTheGapIsPerDomain:
    @pytest.fixture(autouse=True)
    def gap(self, monkeypatch):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("OUTREACH_SEND_SPACING_SECONDS", "60")
        monkeypatch.setenv("OUTREACH_SEND_SPACING_MAX_SECONDS", "60")
        reset_settings_cache()
        yield
        reset_settings_cache()

    def _due(self, session, pool, n):
        sequence = pool["sequence"]
        now = utcnow()
        rows = enroll_candidates(
            session, sequence=sequence, candidate_ids=[c.id for c in pool["candidates"][:n]]
        )
        for row in rows:
            row.next_send_at = now
        session.commit()
        return rows, now

    def test_mailboxes_on_one_domain_share_one_clock(self, session, pool):
        """Providers judge goforay.io, not alpha@ -- three mailboxes on one
        domain sending at once read as one domain sending three at once."""
        _rows, now = self._due(session, pool, 3)

        summary = run_due(session, org_id=pool["sequence"].org_id, now=now)

        assert summary.sent == 1

    def test_different_domains_send_side_by_side(self, session, pool):
        _alpha, bravo, charlie = pool["connections"]
        bravo.address, charlie.address = "bravo@other.io", "charlie@third.io"
        session.commit()
        _rows, now = self._due(session, pool, 3)

        summary = run_due(session, org_id=pool["sequence"].org_id, now=now)

        assert summary.sent == 3
        assert sorted(pool["log"]) == ["alpha@goforay.io", "bravo@other.io", "charlie@third.io"]

    def test_a_follow_up_waits_out_its_domains_gap(self, session, pool):
        alpha, bravo, _charlie = pool["connections"]
        rows, now = self._due(session, pool, 1)
        [row] = rows
        row.mailbox_id = alpha.id  # pinned to alpha
        # A *different* mailbox on the same domain sent 20 seconds ago.
        bravo.last_sent_at = now - timedelta(seconds=20)
        session.commit()

        status, _ = process_enrollment(session, row, now=now)
        assert status == "deferred"
        assert row.gap_retry_at.replace(tzinfo=None) == (
            now + timedelta(seconds=40)
        ).replace(tzinfo=None)

        status, _ = process_enrollment(session, row, now=now + timedelta(seconds=41))
        assert status == "sent"

    def test_rotation_picks_a_mailbox_on_a_domain_that_is_free(self, session, pool):
        alpha, bravo, _charlie = pool["connections"]
        bravo.address = "bravo@other.io"
        now = utcnow()
        alpha.last_sent_at = now - timedelta(seconds=10)  # goforay.io is resting
        session.commit()

        resolved = resolve_sender(session, pool["sequence"], now=now)

        assert not resolved.deferred
        assert resolved.connection.id == bravo.id

    def test_a_resting_domain_is_a_short_wait_not_a_full_pool(self, session, pool):
        now = utcnow()
        pool["connections"][0].last_sent_at = now - timedelta(seconds=30)
        session.commit()

        resolved = resolve_sender(session, pool["sequence"], now=now)

        assert resolved.deferred and resolved.reason == "spacing"

    def test_a_future_stamp_does_not_hold_the_domain(self, session, pool, outreach_fixture):
        """A manual reply filed with a timestamp hours ahead (Zoho's local-time
        sentDateInGMT) must not stop every other mailbox on the domain until
        then. Prod lost the afternoon's sends to exactly this."""
        from juicebox.outreach.rotation import mailbox_gap_wait

        alpha, bravo, _charlie = pool["connections"]
        now = utcnow()
        session.add(
            Message(
                org_id=bravo.org_id,
                candidate_id=outreach_fixture["candidates"][0].id,
                direction="outbound",
                status="sent",
                mailbox_id=bravo.id,
                sent_at=now + timedelta(hours=7),
            )
        )
        session.commit()

        assert mailbox_gap_wait(session, alpha, now) is None
        assert not resolve_sender(session, pool["sequence"], now=now).deferred

        # A real send a moment ago still starts the gap.
        bravo.last_sent_at = now - timedelta(seconds=5)
        session.commit()
        assert mailbox_gap_wait(session, alpha, now) is not None

    def test_the_orgs_own_gap_wins_and_off_means_off(self, session, pool):
        from juicebox.models import Org
        from juicebox.outreach.rotation import mailbox_gap_wait

        alpha = pool["connections"][0]
        org = session.get(Org, alpha.org_id)
        now = utcnow()
        alpha.last_sent_at = now - timedelta(seconds=5)
        org.send_gap_seconds = 40
        session.commit()
        assert mailbox_gap_wait(session, alpha, now).replace(tzinfo=None) == (
            now + timedelta(seconds=35)
        ).replace(tzinfo=None)

        org.send_gap_enabled = False
        session.commit()
        assert mailbox_gap_wait(session, alpha, now) is None


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


class TestSubset:
    """A rotating sequence limited to some of the org's mailboxes.

    The point is negative: the excluded mailbox must never send for this
    sequence, not even when every chosen one is full or gone. Spilling over
    would put outreach on an address the recruiter deliberately kept off it.
    """

    def test_only_the_chosen_mailboxes_send(self, session, pool):
        alpha, bravo, charlie = pool["connections"]
        pool["sequence"].mailbox_ids = [alpha.id, bravo.id]
        session.flush()

        enroll_candidates(
            session,
            sequence=pool["sequence"],
            candidate_ids=[c.id for c in pool["candidates"]],
        )
        session.commit()
        run_due(session, org_id=pool["sequence"].org_id)
        session.commit()

        assert len(pool["log"]) == 3
        assert charlie.address not in pool["log"]
        assert set(pool["log"]) == {alpha.address, bravo.address}

    def test_a_full_subset_defers_instead_of_borrowing_a_spare_mailbox(self, session, pool):
        alpha, _bravo, charlie = pool["connections"]
        pool["sequence"].mailbox_ids = [alpha.id]
        alpha.daily_send_cap = 0
        session.flush()
        # charlie has room, and is not allowed to take it.
        assert charlie.daily_send_cap > 0

        candidate = pool["candidates"][0]
        enroll_candidates(session, sequence=pool["sequence"], candidate_ids=[candidate.id])
        session.commit()
        enrollment = session.query(Enrollment).one()

        status, message = process_enrollment(session, enrollment, now=utcnow())
        assert status == "deferred"
        assert message is None
        assert pool["log"] == []
        assert session.query(Message).count() == 0

    def test_a_subset_whose_mailboxes_are_all_revoked_defers(self, session, pool):
        """Not a fall-through to the global provider: the org still has live
        mailboxes, they are just not this sequence's."""
        alpha, _bravo, _charlie = pool["connections"]
        pool["sequence"].mailbox_ids = [alpha.id]
        alpha.status = "revoked"
        session.flush()

        resolved = resolve_sender(session, pool["sequence"], now=utcnow())
        assert resolved.deferred
        assert resolved.reason == "mailbox_unavailable"
        assert resolved.connection is None

    def test_an_enrollment_already_pinned_outranks_the_subset(self, session, pool):
        """A candidate mid-thread stays on the address that emailed them, even
        if the sequence's subset later excludes it -- moving them would arrive
        as a second email from a stranger."""
        alpha, bravo, _charlie = pool["connections"]
        pool["sequence"].mailbox_ids = [bravo.id]
        session.flush()

        enrollment = Enrollment(
            sequence_id=pool["sequence"].id,
            candidate_id=pool["candidates"][0].id,
            mailbox_id=alpha.id,
        )
        session.add(enrollment)
        session.flush()

        resolved = resolve_sender(session, pool["sequence"], enrollment, now=utcnow())
        assert resolved.connection is not None
        assert resolved.connection.address == alpha.address


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
