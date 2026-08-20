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
from juicebox.integrations.discovery_filters import classify_recipient
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
    def test_a_recipient_becomes_a_reviewable_row(self, session, account, connection):
        _run(
            session,
            connection,
            [[_record(to=["ada.lovelace@acme.com"], days_ago=200)]],
            org_id=account["org_id"],
        )
        row = session.scalar(select(DiscoveredContact))

        assert row.email == "ada.lovelace@acme.com"
        assert row.domain == "acme.com"
        # Humanised from the local part so the tray is readable.
        assert row.display_name == "Ada Lovelace"
        assert row.outbound_count == 1
        assert row.to_count == 1
        assert row.status == "new"
        assert row.suggested_status == "revive"
        assert row.mailbox_ids == [connection.id]

    def test_to_and_cc_are_counted_apart(self, session, account, connection):
        _run(
            session,
            connection,
            [[_record(to=["direct@acme.com"], cc=["copied@acme.com"])]],
            org_id=account["org_id"],
        )
        rows = {r.email: r for r in session.scalars(select(DiscoveredContact)).all()}

        assert rows["direct@acme.com"].to_count == 1
        assert rows["direct@acme.com"].cc_count == 0
        assert rows["copied@acme.com"].cc_count == 1
        # Only ever copied in: the weakest signal, flagged and hidden by default.
        assert rows["copied@acme.com"].suggested_status == "cc_only"

    def test_threads_are_counted_but_never_called_replies(
        self, session, account, connection
    ):
        """thread_count is our own follow-ups as much as anything. Useful to
        sort by, never evidence that somebody answered."""
        _run(
            session,
            connection,
            [
                [
                    _record(to=["ada@acme.com"], thread="t1", native="m1"),
                    _record(to=["ada@acme.com"], thread="t1", native="m2"),
                    _record(to=["ada@acme.com"], thread="t2", native="m3"),
                ]
            ],
            org_id=account["org_id"],
        )
        row = session.scalar(select(DiscoveredContact))
        assert row.outbound_count == 3
        assert row.thread_count == 2

    def test_drafts_are_ignored(self, session, account, connection):
        _run(
            session,
            connection,
            [[_record(to=["ada@acme.com"], draft=True)]],
            org_id=account["org_id"],
        )
        assert session.scalars(select(DiscoveredContact)).all() == []


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

    def test_no_reply_robots_are_dropped_and_role_accounts_are_not(
        self, session, account, connection
    ):
        _run(
            session,
            connection,
            [[_record(to=["no-reply@stripe.com", "support@vendor.com"])]],
            org_id=account["org_id"],
        )
        rows = {r.email: r for r in session.scalars(select(DiscoveredContact)).all()}

        assert "no-reply@stripe.com" not in rows
        # Kept: at a small company info@/support@ is a real person.
        assert rows["support@vendor.com"].suggested_status == "role_account"

    def test_a_mass_cc_is_rejected_as_a_message(self, session, account, connection):
        crowd = [f"person{i}@acme.com" for i in range(25)]
        _run(session, connection, [[_record(to=crowd)]], org_id=account["org_id"])
        assert session.scalars(select(DiscoveredContact)).all() == []

    def test_someone_already_in_contacts_is_staged_as_already_known(
        self, client, session, account, connection
    ):
        """Not skipped: a Foray-imported candidate has no project, sequence or
        message, so their mail has never been ingested. The tray offers to
        import it."""
        created = client.post(
            "/v1/candidates/bulk",
            json={
                "candidates": [
                    {
                        "source": "foray",
                        "source_id": "ada",
                        "full_name": "Ada Lovelace",
                        "emails": ["ada@acme.com"],
                    }
                ]
            },
            headers=account["headers"],
        ).json()

        _run(session, connection, [[_record(to=["ada@acme.com"])]], org_id=account["org_id"])
        row = session.scalar(select(DiscoveredContact))

        assert row.status == "already_known"
        assert row.candidate_id == created["ids"][0]

    def test_an_invalid_address_is_dropped(self, session, account, connection):
        assert classify_recipient("nonsense", own=set(), internal=set()) == "invalid"


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

    def test_an_already_known_row_can_be_promoted(
        self, client, session, account, connection
    ):
        """The bucket is pointless if promoting it is a no-op: it exists so the
        tray can offer to import history for people already in Contacts."""
        client.post(
            "/v1/candidates/bulk",
            json={
                "candidates": [
                    {
                        "source": "foray",
                        "source_id": "ada",
                        "full_name": "Ada Lovelace",
                        "emails": ["ada@acme.com"],
                    }
                ]
            },
            headers=account["headers"],
        )
        _run(session, connection, [[_record(to=["ada@acme.com"])]], org_id=account["org_id"])
        row = session.scalar(select(DiscoveredContact))
        assert row.status == "already_known"

        candidate_id, error = discovery.promote(
            session, row=row, org_id=account["org_id"]
        )

        assert error == ""
        assert session.get(Candidate, candidate_id).crm_tracked is True
        assert row.status == "promoted"

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

    def test_an_unusable_name_is_reported_not_silently_lost(
        self, session, account, connection
    ):
        """upsert_candidates swallows a payload it cannot normalise, so a bulk
        promote could otherwise drop people and still look successful."""
        row = DiscoveredContact(
            org_id=account["org_id"], email="123@acme.com", display_name=""
        )
        session.add(row)
        session.flush()

        # Force the unusable-name path.
        row.display_name = ""
        candidate_id, error = discovery.promote(
            session, row=row, org_id=account["org_id"]
        )
        # Either it made something sensible, or it said why it could not.
        assert candidate_id is not None or error


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
