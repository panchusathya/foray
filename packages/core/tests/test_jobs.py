"""Durable job queue: enqueue idempotency, claim, retry, worker drain."""

from __future__ import annotations

from datetime import timedelta

import pytest

from juicebox.jobs import (
    KIND_OUTREACH_SEND,
    claim,
    enqueue,
    mark_failed,
    recover_stale_locks,
)
from juicebox.models import Enrollment, Job, utcnow
from juicebox.outreach.sequencer import enroll_candidates
from juicebox.worker import drain, process_batch


class TestEnqueue:
    def test_duplicate_key_returns_the_same_row(self, session, account):
        first = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_OUTREACH_SEND,
            idempotency_key="send:abc:step1",
            payload={"n": 1},
        )
        second = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_OUTREACH_SEND,
            idempotency_key="send:abc:step1",
            payload={"n": 2},
        )
        session.commit()
        assert first.id == second.id
        assert session.query(Job).count() == 1
        assert second.payload == {"n": 1}

    @pytest.mark.parametrize("status", ["queued", "running", "succeeded"])
    def test_a_live_or_finished_job_is_handed_back_untouched(
        self, session, account, status
    ):
        """The double-send guarantee: nothing may re-arm one of these."""
        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_OUTREACH_SEND,
            idempotency_key="send:live",
        )
        job.status = status
        job.attempts = 3
        session.commit()

        again = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_OUTREACH_SEND,
            idempotency_key="send:live",
        )
        assert again.id == job.id
        assert again.status == status
        assert again.attempts == 3

    def test_a_parked_job_is_requeued_for_a_later_caller(self, session, account):
        """A half-minute Graph outage must not wedge the enrollment for good.

        Backoff is 2/4/8/16/32s, so all five attempts fit inside one brief
        blip. The row then parked, its send key is permanent, and every
        subsequent tick "re-enqueued" the same dead row and reported
        queued_sends: 1 while the candidate was never written to again.
        """
        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_OUTREACH_SEND,
            idempotency_key="send:parked",
            payload={"attempt": "first"},
        )
        job.status = "failed"
        job.attempts = job.max_attempts
        job.finished_at = utcnow()
        job.last_error = "graph timed out"
        session.commit()

        again = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_OUTREACH_SEND,
            idempotency_key="send:parked",
            payload={"attempt": "second"},
        )
        session.commit()

        assert again.id == job.id  # still one row: the key is still unique
        assert session.query(Job).count() == 1
        assert again.status == "queued"
        assert again.attempts == 0
        assert again.finished_at is None
        assert again.payload == {"attempt": "second"}
        # Kept on purpose: it is the only record of why the job parked.
        assert again.last_error == "graph timed out"

    def test_the_outreach_tick_revives_a_send_it_gave_up_on(
        self, session, outreach_fixture
    ):
        from juicebox.jobs import KIND_OUTREACH_TICK
        from juicebox.worker import _handle_outreach_tick

        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        enrollment = session.query(Enrollment).one()
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]

        send = enqueue(
            session,
            org_id=sequence.org_id,
            kind=KIND_OUTREACH_SEND,
            idempotency_key=f"{KIND_OUTREACH_SEND}:{enrollment.id}:{step.id}",
            payload={
                "org_id": sequence.org_id,
                "enrollment_id": enrollment.id,
                "step_id": step.id,
            },
        )
        send.status = "failed"
        send.attempts = send.max_attempts
        session.commit()

        tick = enqueue(
            session,
            org_id=sequence.org_id,
            kind=KIND_OUTREACH_TICK,
            idempotency_key="tick:1",
            payload={"org_id": sequence.org_id},
        )
        result = _handle_outreach_tick(session, tick, utcnow())

        # It reported one queued send before too -- it just wasn't true.
        assert result["queued_sends"] == 1
        assert send.status == "queued"
        assert send.attempts == 0


class TestClaim:
    def test_claim_marks_running_and_a_second_claim_gets_nothing(self, session, account):
        enqueue(
            session,
            org_id=account["org_id"],
            kind="noop",
            idempotency_key="k1",
        )
        session.commit()
        first = claim(session, worker_id="w1", limit=10)
        assert len(first) == 1 and first[0].status == "running"
        second = claim(session, worker_id="w2", limit=10)
        assert second == []

    def test_stale_lock_is_requeued(self, session, account):
        now = utcnow()
        job = enqueue(
            session,
            org_id=account["org_id"],
            kind="noop",
            idempotency_key="stale",
            run_at=now,
        )
        session.commit()
        claimed = claim(session, worker_id="dead", now=now)
        assert claimed[0].id == job.id
        claimed[0].locked_at = now - timedelta(hours=1)
        session.commit()

        recovered = recover_stale_locks(session, now=now)
        assert recovered == 1
        session.refresh(job)
        assert job.status == "queued"

    def test_failed_job_retries_then_parks(self, session, account):
        job = enqueue(
            session,
            org_id=account["org_id"],
            kind="noop",
            idempotency_key="retry",
            max_attempts=2,
        )
        session.commit()
        mark_failed(job, "boom")
        assert job.status == "queued" and job.attempts == 1
        mark_failed(job, "boom again")
        assert job.status == "failed" and job.attempts == 2


class TestWorkerDrain:
    def test_drain_sends_due_outreach_through_the_queue(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()

        processed = drain(session, worker="test")
        session.commit()

        assert processed >= 1
        assert len(mailbox.outbox) == 1
        assert mailbox.outbox[0].to == "ada@example.com"
        assert any(j.kind == KIND_OUTREACH_SEND and j.status == "succeeded" for j in session.query(Job))

    def test_a_failed_send_does_not_spend_the_idempotency_key(
        self, session, outreach_fixture
    ):
        from juicebox.outreach.sender import SendResult, set_sender
        from juicebox.worker import process_job

        class Boom:
            def send(self, email):
                return SendResult(ok=False, error="smtp timeout")

        set_sender(Boom())
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        enrollment = session.query(Enrollment).one()
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        job = enqueue(
            session,
            org_id=sequence.org_id,
            kind=KIND_OUTREACH_SEND,
            idempotency_key=f"{KIND_OUTREACH_SEND}:{enrollment.id}:{step.id}",
            payload={
                "org_id": sequence.org_id,
                "enrollment_id": enrollment.id,
                "step_id": step.id,
            },
        )
        job.status = "running"
        process_job(session, job)
        session.commit()

        assert job.status == "queued"
        assert job.attempts == 1
        assert "smtp timeout" in job.last_error
        assert enrollment.status == "active"
        assert enrollment.current_step == 0

    def test_process_batch_is_a_no_op_when_the_queue_is_empty(self, session, account):
        assert process_batch(session, worker="test") == 0


class TestInteractiveIsolation:
    """A texter must not wait behind bulk work.

    Live measurement: the general worker was ten minutes into a batch of
    `jobs.parse` calls when a text arrived, and the reply landed 284 seconds
    later -- at the exact second that batch drained. Ordering interactive work
    first inside a claim cannot fix that, because nothing in a process can
    preempt work already running. Hence a separate process, and hence this.
    """

    def test_claim_can_be_restricted_to_kinds(self, session, account):
        from juicebox.jobs import (
            INTERACTIVE_KINDS,
            KIND_INTAKE_TURN,
            KIND_JOBS_PARSE,
            claim,
            enqueue,
        )

        org = account["org_id"]
        enqueue(session, org_id=org, kind=KIND_JOBS_PARSE, idempotency_key="p1", payload={})
        enqueue(session, org_id=org, kind=KIND_INTAKE_TURN, idempotency_key="t1", payload={})
        session.flush()

        got = claim(session, worker_id="w1", kinds=INTERACTIVE_KINDS)
        assert [j.kind for j in got] == [KIND_INTAKE_TURN]

    def test_an_unrestricted_claim_still_takes_everything(self, session, account):
        from juicebox.jobs import KIND_INTAKE_TURN, KIND_JOBS_PARSE, claim, enqueue

        org = account["org_id"]
        enqueue(session, org_id=org, kind=KIND_JOBS_PARSE, idempotency_key="p2", payload={})
        enqueue(session, org_id=org, kind=KIND_INTAKE_TURN, idempotency_key="t2", payload={})
        session.flush()

        kinds = {j.kind for j in claim(session, worker_id="w2")}
        assert kinds == {KIND_JOBS_PARSE, KIND_INTAKE_TURN}

    def test_the_intake_worker_leaves_bulk_work_alone(self, session, account):
        """It must not claim a parse job -- that is the whole point of it."""
        from juicebox.jobs import INTERACTIVE_KINDS, KIND_JOBS_PARSE, enqueue
        from juicebox.worker import process_batch

        enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_JOBS_PARSE,
            idempotency_key="p3",
            payload={},
        )
        session.flush()
        assert process_batch(session, worker="intake", only=INTERACTIVE_KINDS) == 0
