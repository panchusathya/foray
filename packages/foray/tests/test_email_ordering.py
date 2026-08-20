"""emails[0] is the address outreach will send to, so it must be the personal one.

The chain is implicit and easy to break: this lateral's ORDER BY decides the
list order, forayImport.js flattens it in order, the main app's
Candidate.primary_email is emails[0], and send_step sends there. Nothing in
between re-ranks. A recruiting pitch landing in somebody's work inbox is the
failure this guards.
"""

from __future__ import annotations

import asyncpg
import pytest

from foray.api.search import EMAILS_LATERAL
from foray.config import settings
from foray.db import _asyncpg_dsn
from tests.dbutil import requires_db


class TestOrderByClause:
    """Cheap assertions on the SQL itself, so the intent survives a refactor."""

    def test_personal_addresses_sort_first(self):
        assert "order by e.is_work asc" in EMAILS_LATERAL
        assert "order by e.is_work desc" not in EMAILS_LATERAL

    def test_confidence_still_breaks_ties_within_a_group(self):
        assert "e.confidence desc" in EMAILS_LATERAL

    def test_guesses_are_still_excluded(self):
        assert "e.source <> 'guess'" in EMAILS_LATERAL


@requires_db
class TestAgainstPostgres:
    @pytest.fixture
    async def conn(self):
        connection = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
        transaction = connection.transaction()
        await transaction.start()
        try:
            yield connection
        finally:
            await transaction.rollback()
            await connection.close()

    async def _emails_for(self, conn, candidate_id: int):
        row = await conn.fetchrow(
            f"select em.emails from candidates c {EMAILS_LATERAL} where c.id = $1",
            candidate_id,
        )
        import json

        raw = row["emails"]
        return json.loads(raw) if isinstance(raw, str) else (raw or [])

    async def test_a_personal_address_outranks_a_higher_confidence_work_one(self, conn):
        """The exact production shape: a verified work address at confidence 85
        alongside a commit-mined personal one at 75. The personal one must win,
        because confidence answers "is this really them", not "may we email it".
        """
        cid = await conn.fetchval(
            "insert into candidates (login, source) values ('order_test', 'github') returning id"
        )
        await conn.execute(
            "insert into candidate_emails (candidate_id, email, source, is_work, confidence)"
            " values ($1, 'work@stripe.com', 'apollo_work', true, 85),"
            "        ($1, 'me@gmail.com', 'commit', false, 75)",
            cid,
        )
        emails = await self._emails_for(conn, cid)
        assert [e["email"] for e in emails] == ["me@gmail.com", "work@stripe.com"]

    async def test_confidence_orders_within_the_personal_group(self, conn):
        cid = await conn.fetchval(
            "insert into candidates (login, source) values ('order_test2', 'github') returning id"
        )
        await conn.execute(
            "insert into candidate_emails (candidate_id, email, source, is_work, confidence)"
            " values ($1, 'low@gmail.com', 'commit', false, 75),"
            "        ($1, 'high@gmail.com', 'profile', false, 90)",
            cid,
        )
        emails = await self._emails_for(conn, cid)
        assert [e["email"] for e in emails] == ["high@gmail.com", "low@gmail.com"]

    async def test_a_work_only_candidate_still_returns_it(self, conn):
        """Falling back to a work address is correct; leading with one is not."""
        cid = await conn.fetchval(
            "insert into candidates (login, source) values ('order_test3', 'github') returning id"
        )
        await conn.execute(
            "insert into candidate_emails (candidate_id, email, source, is_work, confidence)"
            " values ($1, 'only@stripe.com', 'apollo_work', true, 85)",
            cid,
        )
        emails = await self._emails_for(conn, cid)
        assert [e["email"] for e in emails] == ["only@stripe.com"]

    async def test_a_guess_never_appears(self, conn):
        cid = await conn.fetchval(
            "insert into candidates (login, source) values ('order_test4', 'github') returning id"
        )
        await conn.execute(
            "insert into candidate_emails (candidate_id, email, source, is_work, confidence)"
            " values ($1, 'guessed@stripe.com', 'guess', true, 25),"
            "        ($1, 'real@gmail.com', 'commit', false, 75)",
            cid,
        )
        emails = await self._emails_for(conn, cid)
        assert [e["email"] for e in emails] == ["real@gmail.com"]
