"""Outbox: kill switch, in-flight listing, skip/stop, and the pause-vs-queue bug."""

from __future__ import annotations

from datetime import date, timedelta

from juicebox.jobs import KIND_OUTREACH_SEND, KIND_OUTREACH_TICK, claim, enqueue
from juicebox.models import (
    Candidate,
    Enrollment,
    Job,
    MailboxConnection,
    Message,
    Org,
    Sequence,
    utcnow,
)
from juicebox.outreach.follow_ups import RESULT_SENT, send_follow_up_reminder
from juicebox.outreach.outbox import cancel_in_flight, collect_in_flight, list_in_flight
from juicebox.outreach.sequencer import HOLD_DEFER, enroll_candidates, process_enrollment, run_due
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

    def test_a_held_job_backs_off_five_minutes(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        job = _enqueue_send(session, sequence, enrollment)
        sequence.status = "paused"
        before = utcnow()
        session.commit()
        process_job(session, job, now=before)
        session.commit()

        assert job.status == "queued"
        assert job.run_at >= before + HOLD_DEFER - timedelta(seconds=1)
        assert job.run_at <= before + HOLD_DEFER + timedelta(seconds=1)
        assert mailbox.outbox == []

    def test_a_cap_defer_stays_at_sixty_seconds(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        monkeypatch.setenv("OUTREACH_DAILY_CAP", "0")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        job = _enqueue_send(session, sequence, enrollment)
        before = utcnow()
        session.commit()
        process_job(session, job, now=before)
        session.commit()

        assert job.status == "queued"
        delay = (job.run_at - before).total_seconds()
        assert 30 <= delay <= 90
        assert delay < HOLD_DEFER.total_seconds()
        assert mailbox.outbox == []
        reset_settings_cache()

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
        assert enrollment.next_send_at > utcnow()
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

    def test_stop_cancels_the_job_and_finishes_with_cancelled_by_user(
        self, session, outreach_fixture
    ):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        job = _enqueue_send(session, sequence, enrollment)
        session.commit()

        result = cancel_in_flight(
            session,
            org_id=sequence.org_id,
            enrollment_ids=[enrollment.id],
            action="stop",
        )
        session.commit()
        session.refresh(enrollment)
        session.refresh(job)

        assert result.stopped == 1
        assert job.status == "cancelled"
        assert enrollment.status == "stopped"
        assert enrollment.stopped_reason == "cancelled_by_user"
        assert enrollment.next_send_at is None

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

    def test_skip_of_a_scheduled_row_with_no_job_still_moves_the_enrolment(
        self, session, outreach_fixture
    ):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        enrollment.next_send_at = utcnow() + timedelta(days=2)
        session.commit()

        result = cancel_in_flight(
            session,
            org_id=sequence.org_id,
            enrollment_ids=[enrollment.id],
            action="skip",
        )
        session.commit()
        session.refresh(enrollment)

        assert result.skipped == 1
        assert enrollment.current_step == 1
        assert session.query(Job).filter_by(kind=KIND_OUTREACH_SEND).count() == 0

    def test_a_running_job_is_already_sending_and_still_sends(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        job = _enqueue_send(session, sequence, enrollment)
        job.status = "running"
        session.commit()

        result = cancel_in_flight(
            session,
            org_id=sequence.org_id,
            enrollment_ids=[enrollment.id],
            action="skip",
        )
        session.commit()
        assert result.already_sending == 1
        session.refresh(enrollment)
        assert enrollment.current_step == 0
        session.refresh(job)
        assert job.status == "running"

        process_job(session, job)
        session.commit()
        assert job.status == "succeeded"
        assert len(mailbox.outbox) == 1

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


class TestListing:
    def test_states_and_reasons(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        ada = _ada(outreach_fixture)
        bo = next(c for c in outreach_fixture["candidates"] if c.full_name == "Bo Lindqvist")
        cleo = next(c for c in outreach_fixture["candidates"] if c.full_name == "Cleo Rahman")

        sending = _enroll(session, outreach_fixture, ada)
        sending_job = _enqueue_send(session, sequence, sending)
        sending_job.status = "running"

        due = _enroll(session, outreach_fixture, bo)
        _enqueue_send(session, sequence, due)

        scheduled = _enroll(session, outreach_fixture, cleo)
        scheduled.next_send_at = utcnow() + timedelta(days=3)
        session.commit()

        rows = {row.candidate_name: row for row in collect_in_flight(session, org_id=sequence.org_id)}
        assert rows["Ada Okonkwo"].state == "sending"
        assert rows["Ada Okonkwo"].reason is None
        assert rows["Bo Lindqvist"].state == "due"
        assert rows["Cleo Rahman"].state == "scheduled"
        assert rows["Cleo Rahman"].step == 0
        assert rows["Ada Okonkwo"].mailbox == "talent@acme-talent.com"

    def test_overdue_paused_with_no_job_is_held(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        enrollment.next_send_at = utcnow() - timedelta(hours=2)
        sequence.status = "paused"
        session.commit()

        rows = collect_in_flight(session, org_id=sequence.org_id)
        assert len(rows) == 1
        assert rows[0].state == "held"
        assert rows[0].reason == "sequence_paused"
        assert rows[0].enrollment_id == enrollment.id

    def test_org_pause_marks_queued_rows_held(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        _enqueue_send(session, sequence, enrollment)
        _pause_org(session, sequence.org_id)

        rows = collect_in_flight(session, org_id=sequence.org_id)
        assert rows[0].state == "held"
        assert rows[0].reason == "org_paused"

    def test_daily_cap_reason(self, session, outreach_fixture, monkeypatch):
        monkeypatch.setenv("OUTREACH_DAILY_CAP", "0")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        _enqueue_send(session, sequence, enrollment)
        session.commit()

        rows = collect_in_flight(session, org_id=sequence.org_id)
        assert rows[0].state == "held"
        assert rows[0].reason == "daily_cap"
        reset_settings_cache()

    def test_list_endpoint_paginates(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        for candidate in outreach_fixture["candidates"]:
            _enroll(session, outreach_fixture, candidate)
        total, rows = list_in_flight(
            session, org_id=sequence.org_id, limit=1, offset=0
        )
        assert total == 3
        assert len(rows) == 1


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


class TestSendingApi:
    def test_kill_switch_round_trips(self, client, account):
        headers = account["headers"]
        body = client.get("/v1/org/sending", headers=headers).json()
        assert body["paused"] is False
        assert body["paused_at"] is None
        assert body["held_count"] == 0

        paused = client.patch("/v1/org/sending", json={"paused": True}, headers=headers).json()
        assert paused["paused"] is True
        assert paused["paused_at"]
        assert paused["paused_by_email"] == "recruiter@acme-talent.com"
        first_at = paused["paused_at"]

        again = client.patch("/v1/org/sending", json={"paused": True}, headers=headers).json()
        assert again["paused_at"] == first_at

        live = client.patch("/v1/org/sending", json={"paused": False}, headers=headers).json()
        assert live["paused"] is False
        assert live["paused_at"] is None

    def test_in_flight_list_and_cancel(self, client, session, outreach_fixture, account):
        enrollment = _enroll(session, outreach_fixture)
        session.commit()
        headers = account["headers"]

        listed = client.get("/v1/outreach/in-flight", headers=headers).json()
        assert listed["total"] == 1
        assert listed["rows"][0]["enrollment_id"] == enrollment.id
        assert listed["rows"][0]["state"] in {"due", "scheduled", "held"}

        cancelled = client.post(
            "/v1/outreach/in-flight/cancel",
            json={"enrollment_ids": [enrollment.id], "action": "stop"},
            headers=headers,
        ).json()
        assert cancelled["stopped"] == 1
        session.refresh(enrollment)
        assert enrollment.status == "stopped"
        assert enrollment.stopped_reason == "cancelled_by_user"
