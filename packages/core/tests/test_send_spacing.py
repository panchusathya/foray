"""Sequence emails leave one a minute, however many share a scheduled time."""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from sqlalchemy import select

from juicebox.jobs import KIND_OUTREACH_SEND, KIND_OUTREACH_TICK, enqueue
from juicebox.models import Enrollment, Job, utcnow
from juicebox.outreach.outbox import collect_in_flight
from juicebox.outreach.sequencer import enroll_candidates, run_due
from juicebox.worker import _handle_outreach_tick, process_job

MINUTE = timedelta(minutes=1)


@pytest.fixture(autouse=True)
def spacing(monkeypatch):
    from juicebox.config import reset_settings_cache

    monkeypatch.setenv("OUTREACH_SEND_SPACING_SECONDS", "60")
    reset_settings_cache()
    yield
    reset_settings_cache()


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _enrol_all(session, fixture, *, due):
    sequence = fixture["sequence"]
    rows = enroll_candidates(
        session,
        sequence=sequence,
        candidate_ids=[c.id for c in fixture["candidates"]],
        project_id=sequence.project_id,
    )
    for row in rows:
        row.next_send_at = due
    session.commit()
    return rows


def _tick(session, org_id, now):
    job = Job(org_id=org_id, kind=KIND_OUTREACH_TICK, payload={"org_id": org_id})
    result = _handle_outreach_tick(session, job, now)
    session.commit()
    return result


def _send_jobs(session, org_id):
    return list(
        session.scalars(
            select(Job)
            .where(Job.org_id == org_id, Job.kind == KIND_OUTREACH_SEND)
            .order_by(Job.run_at)
        )
    )


def _job_for(sequence, enrollment):
    step = sorted(sequence.steps, key=lambda s: s.step_order)[enrollment.current_step]
    return f"{KIND_OUTREACH_SEND}:{enrollment.id}:{step.id}"


class TestTheTickDealsSlots:
    def test_people_due_together_are_queued_a_minute_apart(
        self, session, outreach_fixture, mailbox
    ):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow().replace(microsecond=0)
        rows = _enrol_all(session, outreach_fixture, due=now)
        assert len(rows) >= 3

        _tick(session, org_id, now)

        run_ats = [_aware(job.run_at) for job in _send_jobs(session, org_id)]
        assert run_ats == [now + MINUTE * i for i in range(len(rows))]

    def test_a_later_tick_keeps_the_line_and_appends_to_it(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        org_id = sequence.org_id
        now = utcnow().replace(microsecond=0)
        first, *rest = _enrol_all(session, outreach_fixture, due=now)
        # Only the first is due on the first tick.
        for row in rest:
            row.next_send_at = now + timedelta(hours=1)
        session.commit()
        _tick(session, org_id, now)

        later = now + timedelta(seconds=10)
        for row in rest:
            row.next_send_at = later
        session.commit()
        _tick(session, org_id, later)

        by_key = {job.idempotency_key: _aware(job.run_at) for job in _send_jobs(session, org_id)}
        assert by_key[_job_for(sequence, first)] == now
        assert sorted(by_key[_job_for(sequence, row)] for row in rest) == [
            now + MINUTE * (i + 1) for i in range(len(rest))
        ]


class TestTheSendGate:
    def test_a_herd_queued_for_one_instant_sends_one_and_lines_up_the_rest(
        self, session, outreach_fixture, mailbox
    ):
        """Jobs queued before spacing existed, or released together by the
        midnight cap reset, all wake on the same second."""
        sequence = outreach_fixture["sequence"]
        org_id = sequence.org_id
        now = utcnow().replace(microsecond=0)
        rows = _enrol_all(session, outreach_fixture, due=now)
        jobs = [
            enqueue(
                session,
                org_id=org_id,
                kind=KIND_OUTREACH_SEND,
                idempotency_key=_job_for(sequence, row),
                payload={
                    "org_id": org_id,
                    "enrollment_id": row.id,
                    "step_id": sorted(sequence.steps, key=lambda s: s.step_order)[0].id,
                },
                run_at=now,
            )
            for row in rows
        ]
        session.commit()
        for job in jobs:
            job.status = "running"  # one claimed batch, as process_batch does
        session.commit()

        for job in jobs:
            process_job(session, job, now=now)

        assert len(mailbox.outbox) == 1
        waiting = sorted(_aware(job.run_at) for job in jobs if job.status == "queued")
        assert waiting == [now + MINUTE * (i + 1) for i in range(len(rows) - 1)]

    def test_run_now_sends_one_and_leaves_the_rest_for_the_worker(
        self, session, outreach_fixture, mailbox
    ):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        rows = _enrol_all(session, outreach_fixture, due=now)

        summary = run_due(session, org_id=org_id, now=now)
        session.commit()

        assert summary.sent == 1
        assert len(mailbox.outbox) == 1
        still_due = [row for row in rows if session.get(Enrollment, row.id).current_step == 0]
        assert len(still_due) == len(rows) - 1

    def test_the_next_send_goes_once_the_minute_has_passed(
        self, session, outreach_fixture, mailbox
    ):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        _enrol_all(session, outreach_fixture, due=now)

        run_due(session, org_id=org_id, now=now)
        run_due(session, org_id=org_id, now=now + timedelta(seconds=30))
        assert len(mailbox.outbox) == 1
        run_due(session, org_id=org_id, now=now + MINUTE)
        assert len(mailbox.outbox) == 2


class TestTheOutboxShowsTheRealMinute:
    def test_rows_sharing_a_scheduled_time_read_one_minute_apart(
        self, session, outreach_fixture, mailbox
    ):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow().replace(microsecond=0)
        nine = now + timedelta(hours=2)
        rows = _enrol_all(session, outreach_fixture, due=nine)

        items = collect_in_flight(session, org_id=org_id, now=now)

        assert [_aware(item.due_at) for item in items] == [
            nine + MINUTE * i for i in range(len(rows))
        ]
