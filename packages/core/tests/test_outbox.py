"""Outbox: kill switch, in-flight listing, skip/stop, and the pause-vs-queue bug."""

from __future__ import annotations

from datetime import UTC, date

from juicebox.jobs import KIND_OUTREACH_SEND, KIND_OUTREACH_TICK, claim, enqueue
from juicebox.models import (
    Candidate,
    Enrollment,
    MailboxConnection,
    Message,
    Org,
    Sequence,
    utcnow,
)
from juicebox.outreach.follow_ups import RESULT_SENT, send_follow_up_reminder
from juicebox.outreach.outbox import cancel_in_flight
from juicebox.outreach.sequencer import enroll_candidates, process_enrollment, run_due
from juicebox.worker import _handle_outreach_tick, process_job


def _ada(fixture):
    return next(c for c in fixture["candidates"] if c.full_name == "Ada Okonkwo")


def _enroll(session, fixture, candidate=None):
    sequence = fixture["sequence"]
    who = candidate or _ada(fixture)
    [enrollment] = enroll_candidates(
        session, sequence=sequence, candidate_ids=[who.id], project_id=sequence.project_id
    )
    session.commit()
    return session.get(Enrollment, enrollment.id)


def _step(sequence, index=0):
    return sorted(sequence.steps, key=lambda s: s.step_order)[index]


def _enqueue_send(session, sequence, enrollment, *, now=None):
    step = _step(sequence, enrollment.current_step)
    return enqueue(
        session,
        org_id=sequence.org_id,
        kind=KIND_OUTREACH_SEND,
        idempotency_key=f"{KIND_OUTREACH_SEND}:{enrollment.id}:{step.id}",
        payload={
            "org_id": sequence.org_id,
            "enrollment_id": enrollment.id,
            "step_id": step.id,
        },
        run_at=now or utcnow(),
    )


def _pause_org(session, org_id, *, by=None):
    org = session.get(Org, org_id)
    org.outreach_paused_at = utcnow()
    org.outreach_paused_by = by
    session.commit()
    return org


class TestHoldGate:
    def test_a_queued_send_is_held_once_the_sequence_is_paused(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        job = _enqueue_send(session, sequence, enrollment)
        session.commit()

        sequence.status = "paused"
        session.commit()
        process_job(session, job)
        session.commit()

        assert mailbox.outbox == []
        assert job.status == "queued"
        assert job.attempts == 0
        session.refresh(enrollment)
        assert enrollment.status == "active"
        assert enrollment.current_step == 0

    def test_the_org_kill_switch_holds_the_same_way(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        job = _enqueue_send(session, sequence, enrollment)
        session.commit()

        _pause_org(session, sequence.org_id)
        process_job(session, job)
        session.commit()

        assert mailbox.outbox == []
        assert job.status == "queued"
        assert job.attempts == 0

    def test_unpausing_releases_the_held_job(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        job = _enqueue_send(session, sequence, enrollment)
        sequence.status = "paused"
        session.commit()
        process_job(session, job)
        session.commit()
        assert mailbox.outbox == []

        sequence.status = "active"
        job.run_at = utcnow()
        session.commit()
        process_job(session, job)
        session.commit()

        assert job.status == "succeeded"
        assert len(mailbox.outbox) == 1
        assert mailbox.outbox[0].to == "ada@example.com"

    def test_run_due_respects_the_org_hold(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        _enroll(session, outreach_fixture)
        _pause_org(session, sequence.org_id)

        summary = run_due(session, org_id=sequence.org_id)
        session.commit()
        assert summary.sent == 0
        assert mailbox.outbox == []

    def test_send_now_respects_the_hold(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        _pause_org(session, sequence.org_id)
        [enrollment] = enroll_candidates(
            session,
            sequence=sequence,
            candidate_ids=[_ada(outreach_fixture).id],
            send_now=True,
        )
        session.commit()
        status, message = process_enrollment(session, enrollment, now=utcnow())
        session.commit()

        assert status == "deferred"
        assert message is None
        assert mailbox.outbox == []
        assert enrollment.current_step == 0


class TestCancel:
    def test_skip_cancels_the_job_and_schedules_the_next_step(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        job = _enqueue_send(session, sequence, enrollment)
        session.commit()

        result = cancel_in_flight(
            session,
            org_id=sequence.org_id,
            enrollment_ids=[enrollment.id],
            action="skip",
        )
        session.commit()

        assert result.skipped == 1
        session.refresh(job)
        session.refresh(enrollment)
        assert job.status == "cancelled"
        assert enrollment.current_step == 1
        assert enrollment.status == "active"
        assert enrollment.next_send_at is not None
        due = enrollment.next_send_at
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        assert due > utcnow()
        assert mailbox.outbox == []

    def test_skip_of_the_last_step_completes_the_enrolment(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        process_enrollment(session, enrollment, now=utcnow())
        session.commit()
        assert enrollment.current_step == 1
        job = _enqueue_send(session, sequence, enrollment)
        session.commit()

        result = cancel_in_flight(
            session,
            org_id=sequence.org_id,
            enrollment_ids=[enrollment.id],
            action="skip",
        )
        session.commit()
        session.refresh(enrollment)
        session.refresh(job)

        assert result.skipped == 1
        assert job.status == "cancelled"
        assert enrollment.status == "completed"
        assert enrollment.next_send_at is None
        assert len(mailbox.outbox) == 1

    def test_a_cancelled_job_is_never_claimed_and_the_tick_does_not_resurrect_it(
        self, session, outreach_fixture
    ):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        job = _enqueue_send(session, sequence, enrollment)
        session.commit()
        cancel_in_flight(
            session,
            org_id=sequence.org_id,
            enrollment_ids=[enrollment.id],
            action="stop",
        )
        session.commit()

        claimed = claim(session, worker_id="w1", limit=10)
        assert all(row.id != job.id for row in claimed)

        tick = enqueue(
            session,
            org_id=sequence.org_id,
            kind=KIND_OUTREACH_TICK,
            idempotency_key="tick:outbox",
            payload={"org_id": sequence.org_id},
        )
        _handle_outreach_tick(session, tick, utcnow())
        again = enqueue(
            session,
            org_id=sequence.org_id,
            kind=KIND_OUTREACH_SEND,
            idempotency_key=job.idempotency_key,
            payload=job.payload,
        )
        session.commit()
        assert again.id == job.id
        assert again.status == "cancelled"

    def test_cross_org_ids_are_ignored(self, session, outreach_fixture, account):
        other = Org(name="Other", slug="other-outbox")
        session.add(other)
        session.flush()
        candidate = Candidate(
            org_id=other.id,
            full_name="Other Person",
            source_id="other-1",
            emails=["other@example.com"],
        )
        session.add(candidate)
        sequence = Sequence(org_id=other.id, name="Other seq", from_email="x@example.com")
        session.add(sequence)
        session.flush()
        enrollment = Enrollment(
            sequence_id=sequence.id,
            candidate_id=candidate.id,
            status="active",
            next_send_at=utcnow(),
        )
        session.add(enrollment)
        session.commit()

        result = cancel_in_flight(
            session,
            org_id=account["org_id"],
            enrollment_ids=[enrollment.id],
            action="stop",
        )
        session.commit()
        session.refresh(enrollment)
        assert result.not_found == 1
        assert enrollment.status == "active"


class TestFollowUpIsExempt:
    def test_a_follow_up_reminder_still_sends_while_the_org_is_paused(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        from juicebox.outreach import follow_ups

        monkeypatch.setattr(follow_ups, "sender_for_connection", lambda db, conn: mailbox)
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        process_enrollment(session, enrollment, now=utcnow())
        session.commit()

        connection = MailboxConnection(
            org_id=sequence.org_id, address="recruiter@acme-talent.com"
        )
        session.add(connection)
        session.flush()
        enrollment.mailbox_id = connection.id
        enrollment.follow_up_after = date(2026, 9, 10)
        inbound = Message(
            org_id=sequence.org_id,
            enrollment_id=enrollment.id,
            candidate_id=enrollment.candidate_id,
            direction="inbound",
            from_address="ada@example.com",
            to_address=connection.address,
            mailbox_id=connection.id,
            body="In Japan until 9/10 please f/u after",
        )
        session.add(inbound)
        _pause_org(session, sequence.org_id)
        session.commit()

        sent_before = len(mailbox.outbox)
        result = send_follow_up_reminder(session, enrollment=enrollment, inbound=inbound)
        session.commit()
        assert result == RESULT_SENT
        assert len(mailbox.outbox) == sent_before + 1
        assert mailbox.outbox[-1].to == "recruiter@acme-talent.com"


