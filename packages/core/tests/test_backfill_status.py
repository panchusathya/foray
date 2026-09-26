"""Running "Import history" a second time, and being able to see it run.

Reported from production: the button reported success and nothing happened,
with no way to tell what the import was doing or whether it had done anything.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox.jobs import KIND_MAILBOX_BACKFILL
from juicebox.models import Candidate, Job, MailboxConnection, Message, utcnow


@pytest.fixture
def connection(session, account):
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


class TestRepeatRuns:

    def test_a_finished_run_does_not_block_the_next_one(
        self, client, session, account, connection
    ):
        client.post(
            f"/v1/integrations/mailboxes/{connection.id}/backfill",
            headers=account["headers"],
        )
        for job in session.scalars(
            select(Job).where(Job.kind == KIND_MAILBOX_BACKFILL)
        ).all():
            job.status = "succeeded"
        session.commit()

        again = client.post(
            f"/v1/integrations/mailboxes/{connection.id}/backfill",
            headers=account["headers"],
        )
        assert again.status_code == 200, again.text

        open_jobs = session.scalars(
            select(Job).where(
                Job.kind == KIND_MAILBOX_BACKFILL, Job.status.in_(("queued", "running"))
            )
        ).all()
        assert open_jobs, "a completed import must not poison every later run"


class TestHonestResponses:
    def test_thread_sync_off_is_refused_rather_than_silently_dropped(
        self, client, session, account, connection
    ):
        connection.thread_sync_enabled = False
        session.commit()

        response = client.post(
            f"/v1/integrations/mailboxes/{connection.id}/backfill",
            headers=account["headers"],
        )
        assert response.status_code == 409


class TestProgressIsVisible:
    def test_a_run_reports_scope_and_progress(
        self, client, session, account, connection
    ):
        """Going blind was the actual complaint: the import either worked or it
        did not and there was no way to tell which."""
        candidate = Candidate(
            org_id=account["org_id"],
            source="manual",
            source_id="ada",
            full_name="Ada Lovelace",
            emails=["ada@example.com"],
            crm_tracked=True,
        )
        session.add(candidate)
        session.flush()
        session.add(
            Message(
                org_id=account["org_id"],
                candidate_id=candidate.id,
                direction="outbound",
                source="microsoft",
                status="sent",
                sent_at=utcnow() - timedelta(days=5),
            )
        )
        session.commit()

        client.post(
            f"/v1/integrations/mailboxes/{connection.id}/backfill",
            headers=account["headers"],
        )
        session.expire_all()

        state = session.get(MailboxConnection, connection.id).backfill_state
        assert state["status"] == "queued"
        # How many people it intends to look up, so "0 imported" can be told
        # apart from "nobody to import for".
        assert state["total"] >= 1
