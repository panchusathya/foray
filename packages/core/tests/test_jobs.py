"""Durable job queue: enqueue idempotency, claim, retry, worker drain."""

from __future__ import annotations

from datetime import timedelta

from juicebox.jobs import (
    KIND_OUTREACH_SEND,
    claim,
    enqueue,
    mark_failed,
    recover_stale_locks,
)
from juicebox.models import Job, utcnow
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

    def test_process_batch_is_a_no_op_when_the_queue_is_empty(self, session, account):
        assert process_batch(session, worker="test") == 0
