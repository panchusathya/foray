"""Contact discovery: the sweep, the filters, and the promise it must keep.

Two properties dominate this file.

The sweep must not touch the live sync. Its cursor lives in
``discovery_state``; if it ever wrote a delta link, the ongoing sync would
permanently re-read or permanently skip mail, and nothing would look broken
until replies stopped arriving.

And the tray must hold no message content. These are people who have never
been reviewed, let alone opted in.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox.integrations import discovery
from juicebox.integrations.providers import SweepPage, SweepRecord
from juicebox.models import (
    Candidate,
    CandidateEmail,
    DiscoveredContact,
    MailboxConnection,
    Message,
    utcnow,
)


class FakeSweepProvider:
    """Serves canned pages and records what it was asked for."""

    name = "microsoft"

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def iter_sent_history(self, db, connection, *, since, cursor=""):
        self.calls.append({"since": since, "cursor": cursor})
        index = int(cursor or 0)
        if index >= len(self.pages):
            return SweepPage(records=[], next_cursor="", has_more=False)
        records = self.pages[index]
        more = index + 1 < len(self.pages)
        return SweepPage(
            records=records, next_cursor=str(index + 1) if more else "", has_more=more
        )


def _record(to=(), cc=(), *, days_ago=10, thread="t1", native="m1", draft=False):
    return SweepRecord(
        native_id=native,
        from_address="tushar@goforay.io",
        to_addresses=tuple(to),
        cc_addresses=tuple(cc),
        thread_id=thread,
        sent_at=utcnow() - timedelta(days=days_ago),
        is_draft=draft,
    )


@pytest.fixture
def connection(session, account):
    row = MailboxConnection(
        org_id=account["org_id"],
        provider="microsoft",
        address="tushar@goforay.io",
        status="active",
        thread_sync_enabled=True,
        inbox_delta_link="INBOX-DELTA-DO-NOT-TOUCH",
        sent_delta_link="SENT-DELTA-DO-NOT-TOUCH",
        sync_state={"cursors": {"inbox": "zoho-inbox", "sent": "zoho-sent"}},
    )
    session.add(row)
    # Commit, not flush: SQLite locks the file for an open write transaction,
    # and some tests here go through TestClient's own connection.
    session.commit()
    return row


def _run(session, connection, pages, *, org_id):
    provider = FakeSweepProvider(pages)
    now = utcnow()
    context = discovery.build_context(session, org_id=org_id, now=now)
    since = discovery.window_start(now)
    cursor = ""
    while True:
        result = discovery.sweep_page(
            session,
            connection,
            context=context,
            since=since,
            cursor=cursor,
            provider=provider,
        )
        discovery.advance_state(connection, result)
        cursor = result["next_cursor"]
        if not cursor:
            break
    session.flush()
    return provider


class TestTheLiveCursorsAreUntouched:
    def test_a_full_sweep_leaves_every_sync_cursor_byte_identical(
        self, session, account, connection
    ):
        """The single most consequential thing in this feature."""
        before = (
            connection.inbox_delta_link,
            connection.sent_delta_link,
            dict(connection.sync_state or {}),
        )

        _run(
            session,
            connection,
            [[_record(to=["a@acme.com"])], [_record(to=["b@acme.com"], native="m2")]],
            org_id=account["org_id"],
        )

        assert connection.inbox_delta_link == before[0]
        assert connection.sent_delta_link == before[1]
        assert dict(connection.sync_state or {}) == before[2]
        # ...while the sweep's own cursor did move and then finish.
        assert connection.discovery_state["status"] == "done"
        assert connection.discovery_state["scanned"] == 2


class TestNoContentIsStored:
    def test_the_table_has_nowhere_to_put_a_body_or_subject(self):
        """Asserted structurally so adding a column fails loudly."""
        columns = set(DiscoveredContact.__table__.columns.keys())
        assert not columns & {"body", "body_html", "subject", "snippet", "summary"}

    def test_the_sweep_record_cannot_carry_content_either(self):
        fields = set(SweepRecord.__dataclass_fields__)
        assert not fields & {"body", "subject", "bodyPreview", "summary"}

    def test_no_message_rows_are_created_for_strangers(
        self, session, account, connection
    ):
        _run(
            session,
            connection,
            [[_record(to=["stranger@acme.com"])]],
            org_id=account["org_id"],
        )
        assert session.scalars(select(Message)).all() == []


class TestStaging:
    def test_threads_seen_by_an_earlier_job_are_not_forgotten(
        self, session, account, connection
    ):
        """build_context runs once per job and the sweep re-enqueues itself
        across many, so the per-job set of thread ids is not the running total.
        Six threads in job one and one in job two used to leave a count of 1."""
        org_id = account["org_id"]
        _run(
            session,
            connection,
            [
                [
                    _record(to=["ada@acme.com"], thread="t1", native="m1"),
                    _record(to=["ada@acme.com"], thread="t2", native="m2"),
                ]
            ],
            org_id=org_id,
        )
        _run(
            session,
            connection,
            [[_record(to=["ada@acme.com"], thread="t3", native="m3")]],
            org_id=org_id,
        )
        row = session.scalar(select(DiscoveredContact))
        assert row.outbound_count == 3
        assert row.thread_count == 3


class TestFilters:
    def test_our_own_addresses_and_domains_are_excluded(
        self, session, account, connection
    ):
        _run(
            session,
            connection,
            [[_record(to=["tushar@goforay.io", "colleague@goforay.io", "real@acme.com"])]],
            org_id=account["org_id"],
        )
        staged = {r.email for r in session.scalars(select(DiscoveredContact)).all()}
        assert staged == {"real@acme.com"}

    def test_a_gmail_hosted_mailbox_does_not_filter_out_all_of_gmail(
        self, session, account
    ):
        """The bug this guards: deriving internal domains from the connected
        mailbox would make a Gmail-based recruiter discard most of their own
        address book."""
        row = MailboxConnection(
            org_id=account["org_id"],
            provider="microsoft",
            address="recruiter@gmail.com",
            status="active",
            thread_sync_enabled=True,
        )
        session.add(row)
        session.commit()

        _run(
            session,
            row,
            [[_record(to=["candidate@gmail.com", "recruiter@gmail.com"])]],
            org_id=account["org_id"],
        )
        staged = {r.email for r in session.scalars(select(DiscoveredContact)).all()}
        assert staged == {"candidate@gmail.com"}


class TestRerun:
    def test_counters_do_not_double_on_a_second_sweep(
        self, session, account, connection
    ):
        pages = [[_record(to=["ada@acme.com"])]]
        _run(session, connection, pages, org_id=account["org_id"])
        assert session.scalar(select(DiscoveredContact)).outbound_count == 1

        discovery.reset_for_rerun(session, org_id=account["org_id"])
        _run(session, connection, pages, org_id=account["org_id"])

        assert session.scalar(select(DiscoveredContact)).outbound_count == 1

    def test_a_dismissed_vendor_stays_dismissed(self, session, account, connection):
        pages = [[_record(to=["sales@vendor.com"])]]
        _run(session, connection, pages, org_id=account["org_id"])
        row = session.scalar(select(DiscoveredContact))
        discovery.dismiss(session, row=row, reason="vendor")

        discovery.reset_for_rerun(session, org_id=account["org_id"])
        _run(session, connection, pages, org_id=account["org_id"])

        assert session.scalar(select(DiscoveredContact)).status == "dismissed"


class TestPromote:
    def test_promoting_creates_a_tracked_contact_and_opens_the_guard(
        self, session, account, connection
    ):
        from juicebox.integrations.matching import build_address_index

        _run(session, connection, [[_record(to=["ada@acme.com"])]], org_id=account["org_id"])
        row = session.scalar(select(DiscoveredContact))

        assert "ada@acme.com" not in build_address_index(
            session, org_id=account["org_id"]
        )

        candidate_id, error = discovery.promote(
            session, row=row, org_id=account["org_id"]
        )
        assert error == ""
        candidate = session.get(Candidate, candidate_id)

        assert candidate.source == "mailbox"
        # Without this the follow-up import would find nothing at all.
        assert candidate.crm_tracked is True
        assert row.status == "promoted"
        assert "ada@acme.com" in build_address_index(session, org_id=account["org_id"])
        assert session.scalar(
            select(CandidateEmail).where(CandidateEmail.email == "ada@acme.com")
        )


    def test_promoting_twice_is_refused_rather_than_duplicated(
        self, session, account, connection
    ):
        _run(session, connection, [[_record(to=["ada@acme.com"])]], org_id=account["org_id"])
        row = session.scalar(select(DiscoveredContact))
        first, _ = discovery.promote(session, row=row, org_id=account["org_id"])
        second, error = discovery.promote(session, row=row, org_id=account["org_id"])

        assert first is not None
        assert second is None
        assert "already" in error


class TestGuardStillHolds:
    def test_an_unpromoted_stranger_is_still_a_stranger_to_the_sync(
        self, session, account, connection
    ):
        """Staging 40 people must not quietly widen what mail gets stored."""
        from juicebox.integrations.matching import build_address_index

        pages = [[_record(to=[f"p{i}@acme.com"], native=f"m{i}") for i in range(40)]]
        _run(session, connection, pages, org_id=account["org_id"])
        assert len(session.scalars(select(DiscoveredContact)).all()) == 40

        index = build_address_index(session, org_id=account["org_id"])
        assert index == {}
