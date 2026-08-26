"""Where the worker's transaction boundary is, and what happens when it moves.

Every test here is about one property: a job's work and the row that records it
land in the same commit, and nothing else rides along. The whole batch used to
be one transaction, which meant a worker killed on job seventeen rolled the
sixteen before it back to ``queued attempts=0`` while their mail was already
sitting in Graph -- the restart sent it all again.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox import worker
from juicebox.integrations import discovery
from juicebox.jobs import (
    KIND_CRM_DISCOVERY,
    KIND_CRM_DISCOVERY_ONE,
    KIND_PROFILE_INGEST,
    enqueue,
    recover_stale_locks,
)
from juicebox.models import Candidate, Job, MailboxConnection, Org, utcnow

PROBE = "test.probe"
OTHER = "test.probe.other"


def _probes(session, org_id, count):
    """``count`` queued probe jobs, oldest first, all already due."""
    base = utcnow()
    jobs = []
    for n in range(1, count + 1):
        jobs.append(
            enqueue(
                session,
                org_id=org_id,
                kind=PROBE,
                idempotency_key=f"probe:{n}",
                run_at=base - timedelta(seconds=count - n + 1),
            )
        )
    session.commit()
    return jobs


class TestOneJobIsOneTransaction:
    def test_a_kill_mid_batch_keeps_what_finished_and_leaves_the_rest_claimed(
        self, session, account, monkeypatch
    ):
        """probe:1 is done and stays done; probe:2 and 3 are still owned."""
        seen = []

        def handler(db, job, now):
            seen.append(job.idempotency_key)
            if job.idempotency_key == "probe:2":
                # BaseException, so process_job's `except Exception` does not
                # see it: this is the process dying, not a handler failing.
                raise KeyboardInterrupt("kill -9")
            return {"ok": True}

        monkeypatch.setitem(worker.HANDLERS, PROBE, handler)
        _probes(session, account["org_id"], 3)

        with pytest.raises(KeyboardInterrupt):
            worker.process_batch(session, worker="doomed")

        # Everything the dead process had in flight goes away with it. What
        # survives the rollback is what was actually committed.
        session.rollback()

        rows = {j.idempotency_key: j for j in session.scalars(select(Job)).all()}
        assert seen == ["probe:1", "probe:2"]
        assert rows["probe:1"].status == "succeeded"
        assert rows["probe:2"].status == "running"
        assert rows["probe:3"].status == "running"

    def test_a_dead_workers_rows_are_what_recover_stale_locks_requeues(
        self, session, account, monkeypatch
    ):
        """The other half of the same fix: the lock has to be committed to exist.

        While a batch was one transaction a dead worker's rows never reached a
        committed ``running`` state, so this function had nothing to find and
        was, in production, dead code.
        """

        def handler(db, job, now):
            raise KeyboardInterrupt("kill -9")

        monkeypatch.setitem(worker.HANDLERS, PROBE, handler)
        _probes(session, account["org_id"], 2)

        with pytest.raises(KeyboardInterrupt):
            worker.process_batch(session, worker="doomed")
        session.rollback()

        for row in session.scalars(select(Job)).all():
            row.locked_at = utcnow() - timedelta(hours=1)
        session.commit()

        assert recover_stale_locks(session, now=utcnow()) == 2
        assert {j.status for j in session.scalars(select(Job)).all()} == {"queued"}


class TestAFailedHandlerCommitsNothing:
    def test_the_partial_writes_of_a_failed_handler_are_discarded(
        self, session, account, monkeypatch
    ):
        def handler(db, job, now):
            db.get(Org, job.org_id).name = "half written"
            db.flush()
            raise RuntimeError("boom after the write")

        monkeypatch.setitem(worker.HANDLERS, PROBE, handler)
        job = _probes(session, account["org_id"], 1)[0]

        worker.process_job(session, job)
        # What the worker does next, and what used to publish the half-write.
        session.commit()

        assert job.status == "queued"
        assert job.attempts == 1
        assert "boom after the write" in job.last_error
        assert session.get(Org, account["org_id"]).name != "half written"

    def test_an_integrity_error_is_recorded_instead_of_killing_the_worker(
        self, session, account, monkeypatch
    ):
        """The crash-loop bug: a poisoned session used to escape the loop.

        The failing flush left the session needing a rollback, so the next
        statement raised PendingRollbackError out of ``run_loop``'s unguarded
        ``while True``. Nothing had committed, so ``attempts`` never moved, so
        the job could never park -- the supervisor restarted into the same
        failure forever and every org's queue sat behind it.
        """

        def poison(db, job, now):
            db.add(
                Job(
                    org_id=job.org_id,
                    kind="dupe",
                    payload={},
                    idempotency_key=job.idempotency_key,
                )
            )
            db.flush()
            return {}

        monkeypatch.setitem(worker.HANDLERS, PROBE, poison)
        monkeypatch.setitem(worker.HANDLERS, OTHER, lambda db, job, now: {"ok": True})
        _probes(session, account["org_id"], 1)
        enqueue(
            session,
            org_id=account["org_id"],
            kind=OTHER,
            idempotency_key="probe:after",
        )
        session.commit()

        assert worker.process_batch(session, worker="w1") == 2
        session.rollback()

        rows = {j.idempotency_key: j for j in session.scalars(select(Job)).all()}
        assert rows["probe:1"].status == "queued"
        assert rows["probe:1"].attempts == 1
        # The job behind it in the batch still ran, and its result is durable.
        assert rows["probe:after"].status == "succeeded"

    def test_profile_ingest_does_not_keep_its_cache_stamp_when_the_notify_fails(
        self, session, account, monkeypatch
    ):
        """The paid-fetch version of the same bug.

        ``_handle_profile_ingest`` stamps ``profile_ingest_checked_at`` and only
        then texts the candidate. With the stamp committed by a failed run, the
        retry returned ``recently_checked`` and stopped: the profile had been
        bought and the candidate was never told.
        """
        from juicebox.enrich.linkedin_profile import (
            FakeProfileProvider,
            set_profile_provider,
        )
        from juicebox.messaging.channels import WEB, FakeChannel, set_channel
        from juicebox.messaging.threads import open_thread

        url = "https://www.linkedin.com/in/ada-okonkwo"
        payload = {"person": {"fullName": "Ada Okonkwo", "headline": "ML infra"}}
        set_profile_provider(FakeProfileProvider({url: payload}))
        set_channel(WEB, FakeChannel())
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=WEB,
            consent_messaging=True,
            contact_name="Ada Okonkwo",
            contact_email="ada-ingest@example.com",
        )
        session.commit()

        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_PROFILE_INGEST,
            idempotency_key=f"{KIND_PROFILE_INGEST}:ada",
            payload={"linkedin_url": url, "thread_id": thread.id},
        )
        session.commit()

        def boom(*_args, **_kwargs):
            raise RuntimeError("channel refused the text")

        monkeypatch.setattr("juicebox.messaging.notify.notify_candidate", boom)
        worker.process_job(session, job)
        # The commit that used to publish the stamp: without the rollback in
        # the failure path there is still a dirty candidate row sitting here.
        session.commit()

        assert job.status == "queued" and job.attempts == 1
        promoted = session.scalar(
            select(Candidate).where(Candidate.source_id == "ada-okonkwo")
        )
        assert promoted is None or promoted.profile_ingest_checked_at is None

        # The retry must do the work, not skip it as already checked.
        monkeypatch.undo()
        worker.process_job(session, job)
        session.commit()

        assert job.status == "succeeded"
        promoted = session.scalar(
            select(Candidate).where(Candidate.source_id == "ada-okonkwo")
        )
        assert promoted is not None
        assert promoted.profile_ingest_checked_at is not None


class TestRunLoopSurvivesAnUnexpectedError:
    def test_one_bad_iteration_does_not_end_the_process(self, monkeypatch):
        class Stop(BaseException):
            """Not an Exception, so only the test's own break-out sees it."""

        calls = []

        def flaky(db, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("connection reset by peer")
            raise Stop

        monkeypatch.setattr(worker, "process_batch", flaky)
        monkeypatch.setattr("time.sleep", lambda *_: None)

        with pytest.raises(Stop):
            worker.run_loop(poll_seconds=0)

        assert len(calls) == 2


class TestOverlappingDiscoverySweeps:
    @pytest.fixture
    def connection(self, session, account):
        row = MailboxConnection(
            org_id=account["org_id"],
            provider="microsoft",
            address="tushar@goforay.io",
            status="active",
            thread_sync_enabled=True,
        )
        session.add(row)
        session.commit()
        return row

    def test_the_dispatcher_stamps_the_sweep_on_the_connection(
        self, session, account, connection
    ):
        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_CRM_DISCOVERY,
            idempotency_key="sweep:1",
            payload={"org_id": account["org_id"]},
        )
        result = worker._handle_crm_discovery(session, job, utcnow())
        assert result["mailboxes"] == 1
        assert connection.discovery_state["sweep"] == result["sweep"]

    def test_a_page_from_a_superseded_sweep_leaves_the_cursor_alone(
        self, session, account, connection, monkeypatch
    ):
        """Both sweeps share one cursor, so the stale one must not touch it."""
        connection.discovery_state = {
            "cursor": "page-7",
            "sweep": 200,
            "scanned": 0,
            "found": 0,
        }
        session.commit()

        def explode(*_args, **_kwargs):
            raise AssertionError("a superseded sweep must not read a page")

        monkeypatch.setattr(discovery, "sweep_page", explode)

        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_CRM_DISCOVERY_ONE,
            idempotency_key="one:stale",
            payload={
                "org_id": account["org_id"],
                "mailbox_id": connection.id,
                "sweep": 100,
                "page": 0,
            },
        )
        result = worker._handle_crm_discovery_one(session, job, utcnow())

        assert result == {"skipped": "superseded by a newer sweep"}
        assert connection.discovery_state["cursor"] == "page-7"

    def test_the_current_sweeps_own_pages_still_run(
        self, session, account, connection, monkeypatch
    ):
        connection.discovery_state = {
            "cursor": "page-7",
            "sweep": 100,
            "scanned": 0,
            "found": 0,
        }
        session.commit()

        seen = []

        def one_page(db, conn, *, context, since, cursor=""):
            seen.append(cursor)
            return {"scanned": 3, "found": 1, "next_cursor": ""}

        monkeypatch.setattr(discovery, "sweep_page", one_page)

        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_CRM_DISCOVERY_ONE,
            idempotency_key="one:current",
            payload={
                "org_id": account["org_id"],
                "mailbox_id": connection.id,
                "sweep": 100,
                "page": 0,
            },
        )
        result = worker._handle_crm_discovery_one(session, job, utcnow())

        assert seen == ["page-7"]
        assert result["scanned"] == 3
        assert connection.discovery_state["sweep"] == 100
