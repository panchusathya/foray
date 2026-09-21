"""Re-enrol from the outbox: undoing a stop, and what it must never undo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from juicebox.jobs import KIND_OUTREACH_SEND, KIND_OUTREACH_TICK, enqueue
from juicebox.models import Enrollment, Event, Job, Message, utcnow
from juicebox.outreach.outbox import (
    STOPPED_BY_USER,
    cancel_in_flight,
    cancelled_count,
    collect_cancelled,
    collect_in_flight,
    list_in_flight,
    reenroll_in_flight,
)
from juicebox.outreach.sequencer import enroll_candidates, process_enrollment, record_reply
from juicebox.worker import _handle_outreach_tick, process_job


def _candidate(fixture, name):
    return next(c for c in fixture["candidates"] if c.full_name == name)


def _enroll(session, fixture, candidate=None):
    sequence = fixture["sequence"]
    who = candidate or _candidate(fixture, "Ada Okonkwo")
    [enrollment] = enroll_candidates(
        session, sequence=sequence, candidate_ids=[who.id], project_id=sequence.project_id
    )
    session.commit()
    return session.get(Enrollment, enrollment.id)


def _step(sequence, index):
    return sorted(sequence.steps, key=lambda s: s.step_order)[index]


def _send_key(sequence, enrollment):
    step = _step(sequence, enrollment.current_step)
    return f"{KIND_OUTREACH_SEND}:{enrollment.id}:{step.id}"


def _queue_send(session, sequence, enrollment):
    step = _step(sequence, enrollment.current_step)
    return enqueue(
        session,
        org_id=sequence.org_id,
        kind=KIND_OUTREACH_SEND,
        idempotency_key=_send_key(sequence, enrollment),
        payload={
            "org_id": sequence.org_id,
            "enrollment_id": enrollment.id,
            "step_id": step.id,
        },
        run_at=utcnow(),
    )


def _stop(session, sequence, enrollment):
    """The outbox's own stop: cancel the queued job and finish the enrolment."""
    job = _queue_send(session, sequence, enrollment)
    session.commit()
    result = cancel_in_flight(
        session, org_id=sequence.org_id, enrollment_ids=[enrollment.id], action="stop"
    )
    session.commit()
    assert result.stopped == 1
    session.refresh(enrollment)
    assert enrollment.status == "stopped"
    assert enrollment.stopped_reason == STOPPED_BY_USER
    return job


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class TestReenroll:
    def test_a_stopped_row_comes_back_active_at_the_stopped_step(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        process_enrollment(session, enrollment, now=utcnow())
        session.commit()
        assert enrollment.current_step == 1
        job = _stop(session, sequence, enrollment)
        now = utcnow()

        result = reenroll_in_flight(
            session, org_id=sequence.org_id, enrollment_ids=[enrollment.id], now=now
        )
        session.commit()
        session.refresh(enrollment)

        assert result.resumed == 1
        assert result.not_found == 0
        assert enrollment.status == "active"
        assert enrollment.stopped_reason == ""
        # Resumes where it stopped: the first step already went out, and a
        # re-enrol must not send it again.
        assert enrollment.current_step == 1
        assert enrollment.next_send_at is not None
        # The follow-up has a 3-day delay and no fixed hour: the default
        # ignores the delay (it was served before the stop) and lands now.
        assert abs((_aware(enrollment.next_send_at) - now).total_seconds()) < 5
        # The cancelled job is gone, so the tick can queue a fresh one.
        assert session.get(Job, job.id) is None

    def test_the_tick_then_queues_a_fresh_send_and_it_goes_out(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        cancelled = _stop(session, sequence, enrollment)
        key = cancelled.idempotency_key

        reenroll_in_flight(
            session, org_id=sequence.org_id, enrollment_ids=[enrollment.id], when="now"
        )
        session.commit()

        tick = enqueue(
            session,
            org_id=sequence.org_id,
            kind=KIND_OUTREACH_TICK,
            idempotency_key="tick:reenrol",
            payload={"org_id": sequence.org_id},
        )
        summary = _handle_outreach_tick(session, tick, utcnow())
        session.commit()
        assert summary["queued_sends"] == 1

        fresh = session.scalar(session.query(Job).filter_by(idempotency_key=key).statement)
        assert fresh is not None
        assert fresh.id != cancelled.id
        assert fresh.status == "queued"

        process_job(session, fresh)
        session.commit()
        assert fresh.status == "succeeded"
        assert len(mailbox.outbox) == 1
        assert mailbox.outbox[0].to == "ada@example.com"
        session.refresh(enrollment)
        assert enrollment.current_step == 1

    def test_at_schedules_for_the_given_time_and_reads_as_scheduled(
        self, session, outreach_fixture
    ):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        _stop(session, sequence, enrollment)
        thursday = (utcnow() + timedelta(days=2)).replace(microsecond=0)

        result = reenroll_in_flight(
            session,
            org_id=sequence.org_id,
            enrollment_ids=[enrollment.id],
            when="at",
            send_at=thursday,
        )
        session.commit()
        session.refresh(enrollment)

        assert result.resumed == 1
        assert _aware(enrollment.next_send_at) == thursday
        rows = collect_in_flight(session, org_id=sequence.org_id)
        assert [row.state for row in rows] == ["scheduled"]
        assert _aware(rows[0].due_at) == thursday
        # And it has left the cancelled population.
        assert collect_cancelled(session, org_id=sequence.org_id) == []
        assert cancelled_count(session, org_id=sequence.org_id) == 0

    def test_next_slot_lands_on_the_steps_own_hour_without_the_delay(
        self, session, outreach_fixture
    ):
        sequence = outreach_fixture["sequence"]
        follow_up = _step(sequence, 1)
        follow_up.send_at_time = "09:00"
        follow_up.send_timezone = "UTC"
        session.commit()

        enrollment = _enroll(session, outreach_fixture)
        process_enrollment(session, enrollment, now=utcnow())
        session.commit()
        _stop(session, sequence, enrollment)

        now = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)  # a Monday afternoon
        reenroll_in_flight(session, org_id=sequence.org_id, enrollment_ids=[enrollment.id], now=now)
        session.commit()
        session.refresh(enrollment)
        # Tomorrow 09:00, not three days out: the 3-day delay was already
        # served before the stop.
        assert _aware(enrollment.next_send_at) == datetime(2026, 9, 22, 9, 0, tzinfo=UTC)

    def test_at_requires_a_time(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        with pytest.raises(ValueError):
            reenroll_in_flight(session, org_id=sequence.org_id, enrollment_ids=["enr_x"], when="at")

    def test_it_writes_an_event(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        _stop(session, sequence, enrollment)
        reenroll_in_flight(session, org_id=sequence.org_id, enrollment_ids=[enrollment.id])
        session.commit()

        event = session.query(Event).filter_by(type="outreach.reenrolled").one()
        assert event.subject_id == enrollment.id
        assert event.payload["when"] == "next_slot"
        assert event.payload["step"] == 0


class TestWhatItRefuses:
    def test_only_operator_stops_qualify(self, session, outreach_fixture, mailbox):
        """A reply-stopped enrolment must stay stopped whatever the outbox is told."""
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        process_enrollment(session, enrollment, now=utcnow())
        session.commit()
        sent = session.query(Message).filter_by(direction="outbound").one()
        record_reply(session, message=sent, subject="Re: hi", body="Not interested, thanks.")
        session.commit()
        session.refresh(enrollment)
        assert enrollment.status == "replied"

        result = reenroll_in_flight(
            session, org_id=sequence.org_id, enrollment_ids=[enrollment.id], when="now"
        )
        session.commit()
        session.refresh(enrollment)
        assert result.resumed == 0
        assert result.not_found == 1
        assert enrollment.status == "replied"

    def test_an_active_row_is_not_found_either(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        before = enrollment.next_send_at
        result = reenroll_in_flight(
            session, org_id=sequence.org_id, enrollment_ids=[enrollment.id], when="now"
        )
        session.commit()
        session.refresh(enrollment)
        assert result.not_found == 1
        # SQLite hands the column back naive; compare on equal footing.
        assert _aware(enrollment.next_send_at) == _aware(before)

    def test_someone_who_replied_by_hand_elsewhere_is_reported_not_resumed(
        self, session, outreach_fixture, mailbox
    ):
        """Cancelled on this sequence, in conversation on another: still a conversation."""
        from juicebox.models import Sequence, SequenceStep

        sequence = outreach_fixture["sequence"]
        ada = _candidate(outreach_fixture, "Ada Okonkwo")
        enrollment = _enroll(session, outreach_fixture)
        _stop(session, sequence, enrollment)

        other = Sequence(
            org_id=sequence.org_id,
            project_id=sequence.project_id,
            name="Other touch",
            from_email="talent@acme-talent.com",
            personalize=False,
        )
        session.add(other)
        session.flush()
        session.add(
            SequenceStep(
                sequence_id=other.id,
                step_order=0,
                delay_days=0,
                subject_template="Hello {{ first_name }}",
                body_template="Hi {{ first_name }}",
            )
        )
        session.commit()
        [other_enrollment] = enroll_candidates(session, sequence=other, candidate_ids=[ada.id])
        session.commit()
        process_enrollment(session, other_enrollment, now=utcnow())
        session.commit()
        sent = session.query(Message).filter_by(enrollment_id=other_enrollment.id).one()
        record_reply(
            session, message=sent, subject="Re: Hello", body="Sounds interesting, tell me more."
        )
        session.commit()

        result = reenroll_in_flight(session, org_id=sequence.org_id, enrollment_ids=[enrollment.id])
        session.commit()
        session.refresh(enrollment)
        assert result.resumed == 0
        assert [row.candidate_id for row in result.replied] == [ada.id]
        assert result.replied[0].full_name == "Ada Okonkwo"
        assert enrollment.status == "stopped"

        forced = reenroll_in_flight(
            session, org_id=sequence.org_id, enrollment_ids=[enrollment.id], force=True
        )
        session.commit()
        session.refresh(enrollment)
        assert forced.resumed == 1
        assert enrollment.status == "active"

    def test_cross_org_ids_are_ignored(self, session, outreach_fixture, account):
        from juicebox.models import Candidate, Org, Sequence

        other = Org(name="Other", slug="other-reenrol")
        session.add(other)
        session.flush()
        candidate = Candidate(
            org_id=other.id, full_name="Other Person", source_id="o-1", emails=["o@example.com"]
        )
        session.add(candidate)
        sequence = Sequence(org_id=other.id, name="Other seq", from_email="x@example.com")
        session.add(sequence)
        session.flush()
        enrollment = Enrollment(
            sequence_id=sequence.id,
            candidate_id=candidate.id,
            status="stopped",
            stopped_reason=STOPPED_BY_USER,
        )
        session.add(enrollment)
        session.commit()

        result = reenroll_in_flight(
            session, org_id=account["org_id"], enrollment_ids=[enrollment.id], when="now"
        )
        session.commit()
        session.refresh(enrollment)
        assert result.not_found == 1
        assert enrollment.status == "stopped"
        assert collect_cancelled(session, org_id=account["org_id"]) == []


class TestCancelledListing:
    def test_stopped_rows_show_under_cancelled_and_nowhere_else(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        ada = _candidate(outreach_fixture, "Ada Okonkwo")
        bo = _candidate(outreach_fixture, "Bo Lindqvist")
        stopped = _enroll(session, outreach_fixture, ada)
        _enroll(session, outreach_fixture, bo)
        _stop(session, sequence, stopped)

        total, live = list_in_flight(session, org_id=sequence.org_id)
        assert total == 1
        assert live[0].candidate_name == "Bo Lindqvist"

        total, rows = list_in_flight(session, org_id=sequence.org_id, state="cancelled")
        assert total == 1
        [row] = rows
        assert row.enrollment_id == stopped.id
        assert row.state == "cancelled"
        assert row.due_at is None
        assert row.cancelled_at is not None
        assert row.step == 0
        assert row.mailbox == "talent@acme-talent.com"
        assert cancelled_count(session, org_id=sequence.org_id) == 1
        assert cancelled_count(session, org_id=sequence.org_id, sequence_id="seq_none") == 0


class TestApi:
    def test_list_then_reenrol_for_a_chosen_time(self, client, session, outreach_fixture, account):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        _stop(session, sequence, enrollment)
        headers = account["headers"]

        live = client.get("/v1/outreach/in-flight", headers=headers).json()
        assert live["total"] == 0
        assert live["cancelled_total"] == 1

        cancelled = client.get("/v1/outreach/in-flight?state=cancelled", headers=headers).json()
        assert cancelled["total"] == 1
        assert cancelled["rows"][0]["state"] == "cancelled"
        assert cancelled["rows"][0]["cancelled_at"]

        thursday = (utcnow() + timedelta(days=2)).replace(microsecond=0)
        body = client.post(
            "/v1/outreach/in-flight/reenroll",
            json={
                "enrollment_ids": [enrollment.id],
                "when": "at",
                "send_at": thursday.isoformat(),
            },
            headers=headers,
        ).json()
        assert body["resumed"] == 1
        assert body["sent"] == 0
        assert body["skipped"] == []
        assert datetime.fromisoformat(body["next_send_at"].replace("Z", "+00:00")) == thursday

        session.refresh(enrollment)
        assert enrollment.status == "active"
        live = client.get("/v1/outreach/in-flight", headers=headers).json()
        assert live["total"] == 1
        assert live["rows"][0]["state"] == "scheduled"
        assert live["cancelled_total"] == 0

    def test_now_sends_in_the_request(self, client, session, outreach_fixture, account, mailbox):
        sequence = outreach_fixture["sequence"]
        enrollment = _enroll(session, outreach_fixture)
        _stop(session, sequence, enrollment)

        body = client.post(
            "/v1/outreach/in-flight/reenroll",
            json={"enrollment_ids": [enrollment.id], "when": "now"},
            headers=account["headers"],
        ).json()
        assert body["resumed"] == 1
        assert body["sent"] == 1
        assert len(mailbox.outbox) == 1
        session.refresh(enrollment)
        assert enrollment.current_step == 1

    def test_at_without_a_time_is_rejected(self, client, account):
        response = client.post(
            "/v1/outreach/in-flight/reenroll",
            json={"enrollment_ids": ["enr_x"], "when": "at"},
            headers=account["headers"],
        )
        assert response.status_code == 422

    def test_a_viewer_cannot_reenrol(self, client, session, account):
        from juicebox.models import User

        user = session.get(User, account["user_id"])
        user.role = "viewer"
        session.commit()
        response = client.post(
            "/v1/outreach/in-flight/reenroll",
            json={"enrollment_ids": ["enr_x"], "when": "now"},
            headers=account["headers"],
        )
        assert response.status_code == 403
