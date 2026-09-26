"""Where a discovered contact's name comes from.

Reported from production: about half the imported contacts had no name. The
cause was that both providers hand us the display name from the To: header and
both threw it away, leaving discovery to guess from the email local part --
which only ever works for first.last.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox.integrations import discovery
from juicebox.integrations.discovery import display_name_from
from juicebox.integrations.microsoft import to_sweep_record
from juicebox.integrations.providers import SweepPage, SweepRecord
from juicebox.integrations.zoho import _named_addresses
from juicebox.models import DiscoveredContact, MailboxConnection, utcnow


class FakeProvider:
    name = "microsoft"

    def __init__(self, pages):
        self.pages = pages

    def iter_sent_history(self, db, connection, *, since, cursor=""):
        index = int(cursor or 0)
        if index >= len(self.pages):
            return SweepPage()
        more = index + 1 < len(self.pages)
        return SweepPage(
            records=self.pages[index],
            next_cursor=str(index + 1) if more else "",
            has_more=more,
        )


def _record(to, names=None, *, native="m1", days=10):
    return SweepRecord(
        native_id=native,
        from_address="tushar@goforay.io",
        to_addresses=tuple(to),
        thread_id=native,
        sent_at=utcnow() - timedelta(days=days),
        names=names or {},
    )


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


def _sweep(session, connection, pages, *, org_id):
    provider = FakeProvider(pages)
    now = utcnow()
    context = discovery.build_context(session, org_id=org_id, now=now)
    since = discovery.window_start(now)
    cursor = ""
    while True:
        result = discovery.sweep_page(
            session, connection, context=context, since=since,
            cursor=cursor, provider=provider,
        )
        cursor = result["next_cursor"]
        if not cursor:
            break
    session.flush()


class TestTheProvidersKeepTheName:
    def test_graph_keeps_the_display_name(self):
        record = to_sweep_record(
            {
                "id": "m1",
                "from": {"emailAddress": {"address": "me@goforay.io"}},
                "toRecipients": [
                    {"emailAddress": {"address": "Adia@Acme.com", "name": "Adia Chenumbay"}}
                ],
                "ccRecipients": [
                    {"emailAddress": {"address": "cc@acme.com", "name": "Cc Person"}}
                ],
            }
        )
        assert record.names["adia@acme.com"] == "Adia Chenumbay"
        assert record.names["cc@acme.com"] == "Cc Person"
        # ...without losing the separation the sweep depends on.
        assert record.to_addresses == ("adia@acme.com",)
        assert record.cc_addresses == ("cc@acme.com",)

    def test_zoho_keeps_the_display_name(self):
        assert _named_addresses("Adia Chenumbay <adia@acme.com>") == [
            ("adia@acme.com", "Adia Chenumbay")
        ]

    def test_a_comma_inside_a_quoted_name_is_not_a_separator(self):
        """"Smith, John" <js@x.com> is one recipient. Splitting naively makes
        it two and leaves the real address carrying only half a name."""
        assert _named_addresses('"Smith, John" <js@acme.com>, bare@acme.com') == [
            ("js@acme.com", "Smith, John"),
            ("bare@acme.com", ""),
        ]

    def test_the_sweep_record_still_cannot_carry_content(self):
        # A name on a header is identity. A subject or a body is not, and the
        # tray still has nowhere to put one.
        fields = set(SweepRecord.__dataclass_fields__)
        assert "names" in fields
        assert not fields & {"body", "subject", "bodyPreview", "summary"}


class TestGuessingIsALastResort:
    def test_the_header_name_beats_the_local_part(
        self, session, account, connection
    ):
        _sweep(
            session,
            connection,
            [[_record(["a.person@acme.com"], {"a.person@acme.com": "Adia Chenumbay"})]],
            org_id=account["org_id"],
        )
        row = session.scalar(select(DiscoveredContact))
        # Not "A Person", which is what the local part would have produced.
        assert row.display_name == "Adia Chenumbay"

    @pytest.mark.parametrize(
        "local", ["achenumbay", "jsmith", "hr", "contact", "adia2", "adia.c2"]
    )
    def test_a_name_is_never_invented(self, local):
        """A wrong name is worse than a missing one: it looks authoritative,
        it is what goes into outreach, and nobody goes looking to correct it."""
        assert display_name_from(f"{local}@acme.com") == ""


class TestBestNameWins:
    def test_a_later_terse_header_does_not_overwrite_a_full_name(
        self, session, account, connection
    ):
        _sweep(
            session,
            connection,
            [
                [
                    _record(["adia@acme.com"], {"adia@acme.com": "Adia Chenumbay"}, native="m1"),
                    _record(["adia@acme.com"], {"adia@acme.com": "adia"}, native="m2"),
                ]
            ],
            org_id=account["org_id"],
        )
        assert session.scalar(select(DiscoveredContact)).display_name == "Adia Chenumbay"
