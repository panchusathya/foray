"""The paid route: who may call it, and when it declines to spend.

``/foray/*`` is unauthenticated end to end, so the token check here is the only
thing between a stranger and our ContactOut balance. The triage tests are the
other half: every branch that answers without a lookup is a credit not spent,
and the failure mode -- paying twice for the same answer -- is invisible until
the invoice arrives.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from fastapi import HTTPException

from foray.api import internal
from foray.config import settings
from foray.db import _asyncpg_dsn
from foray.enrich.contacts import FoundEmail, ProviderResult
from tests.dbutil import requires_db


class RecordingProvider:
    """Satisfies EmailProvider; records who it was asked about."""

    name = "contactout"

    def __init__(self, result: ProviderResult | None = None, *, configured: bool = True) -> None:
        self._result = result or ProviderResult()
        self._configured = configured
        self.asked: list[str] = []

    def is_configured(self) -> bool:
        return self._configured

    async def find_emails(self, node: dict) -> ProviderResult:
        self.asked.append(node["login"])
        return self._result


def row(login, *, linkedin_url="https://www.linkedin.com/in/x", checked=False, has_personal=False):
    """A stand-in for the asyncpg Record that SELECT_TARGETS returns."""
    return {
        "id": abs(hash(login)) % 10_000,
        "login": login,
        "linkedin_url": linkedin_url,
        "checked": checked,
        "has_personal": has_personal,
    }


class TestTokenGate:
    def test_a_missing_token_is_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "foray_internal_token", "s3cret")
        with pytest.raises(HTTPException) as exc:
            internal._require_token(None)
        assert exc.value.status_code == 401

    def test_a_wrong_token_is_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "foray_internal_token", "s3cret")
        with pytest.raises(HTTPException) as exc:
            internal._require_token("nearly-s3cret")
        assert exc.value.status_code == 401

    def test_the_right_token_passes(self, monkeypatch):
        monkeypatch.setattr(settings, "foray_internal_token", "s3cret")
        internal._require_token("s3cret")  # no raise

    def test_an_unconfigured_deployment_is_503_not_a_dead_worker(self, monkeypatch):
        """settings.require() raises SystemExit, which inside a request handler
        takes the process down instead of answering. A 503 says what is wrong."""
        monkeypatch.setattr(settings, "foray_internal_token", "")
        with pytest.raises(HTTPException) as exc:
            internal._require_token("anything")
        assert exc.value.status_code == 503
        assert "FORAY_INTERNAL_TOKEN" in exc.value.detail

    def test_an_unconfigured_deployment_refuses_even_an_empty_token(self, monkeypatch):
        """No token configured must never mean "no token required"."""
        monkeypatch.setattr(settings, "foray_internal_token", "")
        with pytest.raises(HTTPException):
            internal._require_token(None)


class TestTriage:
    def test_a_fresh_candidate_is_worth_a_lookup(self):
        results, to_look_up = internal.triage(["ada"], [row("ada")])
        assert results == {}
        assert [r["login"] for r in to_look_up] == ["ada"]

    def test_an_already_checked_candidate_is_not_paid_for_twice(self):
        """The whole reason contactout_checked_at exists."""
        results, to_look_up = internal.triage(["ada"], [row("ada", checked=True)])
        assert results == {"ada": {"status": "cached"}}
        assert to_look_up == []

    def test_a_candidate_who_already_has_a_personal_address_is_skipped(self):
        results, to_look_up = internal.triage(["ada"], [row("ada", has_personal=True)])
        assert results == {"ada": {"status": "has_personal"}}
        assert to_look_up == []

    def test_no_profile_url_means_nothing_to_match(self):
        results, to_look_up = internal.triage(["ada"], [row("ada", linkedin_url=None)])
        assert results == {"ada": {"status": "no_profile"}}
        assert to_look_up == []

    def test_a_login_foray_has_never_seen_is_reported_not_guessed_at(self):
        results, to_look_up = internal.triage(["ghost"], [])
        assert results == {"ghost": {"status": "unknown"}}
        assert to_look_up == []

    def test_having_an_address_wins_over_never_having_been_checked(self):
        """Order matters: has_personal is tested first, so a candidate whose
        address came from commit mining is never looked up at all."""
        results, _ = internal.triage(["ada"], [row("ada", has_personal=True, checked=False)])
        assert results["ada"]["status"] == "has_personal"

    def test_a_mixed_batch_splits_correctly(self):
        rows = [
            row("fresh"),
            row("done", checked=True),
            row("covered", has_personal=True),
            row("anonymous", linkedin_url=None),
        ]
        results, to_look_up = internal.triage(
            ["fresh", "done", "covered", "anonymous", "ghost"], rows
        )
        assert [r["login"] for r in to_look_up] == ["fresh"]
        assert {k: v["status"] for k, v in results.items()} == {
            "done": "cached",
            "covered": "has_personal",
            "anonymous": "no_profile",
            "ghost": "unknown",
        }


class TestLookUp:
    def test_nothing_to_look_up_makes_no_request(self):
        provider = RecordingProvider()
        assert asyncio.run(internal._look_up(provider, [])) == []
        assert provider.asked == []

    def test_a_failing_lookup_does_not_take_the_batch_down(self):
        """One vendor error must not lose the addresses we already paid for."""

        class Flaky(RecordingProvider):
            async def find_emails(self, node):
                self.asked.append(node["login"])
                if node["login"] == "boom":
                    raise RuntimeError("connection reset")
                return ProviderResult(emails=[FoundEmail("a@gmail.com", is_work=False)])

        provider = Flaky()
        out = asyncio.run(internal._look_up(provider, [row("ok"), row("boom"), row("ok2")]))
        statuses = {r["login"]: (result is not None) for r, result, _ in out}
        assert statuses == {"ok": True, "boom": False, "ok2": True}
        assert sorted(provider.asked) == ["boom", "ok", "ok2"]

    def test_an_ordinary_failure_is_not_reported_as_provider_unavailable(self):
        class Broken(RecordingProvider):
            async def find_emails(self, node):
                raise RuntimeError("connection reset")

        [(_, result, failure)] = asyncio.run(internal._look_up(Broken(), [row("x")]))
        assert result is None
        assert not failure.startswith(internal._UNAVAILABLE)


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

    async def _make(self, conn, login, *, linkedin_url=None, email=None, is_work=False):
        cid = await conn.fetchval(
            "insert into candidates (login, source, linkedin_url) values ($1, 'github', $2)"
            " returning id",
            login,
            linkedin_url,
        )
        if email:
            await conn.execute(
                "insert into candidate_emails (candidate_id, email, source, is_work, confidence)"
                " values ($1, $2, $3, $4, 75)",
                cid,
                email,
                "commit" if not is_work else "apollo_work",
                is_work,
            )
        return cid

    async def test_the_query_reports_what_triage_needs(self, conn):
        await self._make(conn, "fresh_1", linkedin_url="https://www.linkedin.com/in/a")
        rows = await conn.fetch(internal.SELECT_TARGETS, ["fresh_1"])
        assert len(rows) == 1
        assert rows[0]["checked"] is False
        assert rows[0]["has_personal"] is False
        assert rows[0]["linkedin_url"] == "https://www.linkedin.com/in/a"

    async def test_a_work_only_candidate_still_counts_as_uncovered(self, conn):
        """A work address is not the thing we are buying, so a candidate who has
        only one is still worth a lookup."""
        await self._make(conn, "worky_1", email="a@stripe.com", is_work=True)
        [record] = await conn.fetch(internal.SELECT_TARGETS, ["worky_1"])
        assert record["has_personal"] is False

    async def test_a_commit_mined_address_counts_as_covered(self, conn):
        await self._make(conn, "covered_1", email="a@gmail.com")
        [record] = await conn.fetch(internal.SELECT_TARGETS, ["covered_1"])
        assert record["has_personal"] is True

    async def test_a_guess_does_not_count_as_covered(self, conn):
        """A guessed address is excluded from search results, so treating it as
        coverage would leave the candidate permanently unreachable."""
        cid = await self._make(conn, "guessed_1")
        await conn.execute(
            "insert into candidate_emails (candidate_id, email, source, is_work, confidence)"
            " values ($1, 'guess@gmail.com', 'guess', false, 25)",
            cid,
        )
        [record] = await conn.fetch(internal.SELECT_TARGETS, ["guessed_1"])
        assert record["has_personal"] is False

    async def test_the_stamp_makes_the_second_call_free(self, conn):
        cid = await self._make(conn, "stamped_1", linkedin_url="https://www.linkedin.com/in/a")
        await conn.execute(
            "update candidates set contactout_checked_at = now() where id = $1", cid
        )
        [record] = await conn.fetch(internal.SELECT_TARGETS, ["stamped_1"])
        assert record["checked"] is True
        _, to_look_up = internal.triage(["stamped_1"], [record])
        assert to_look_up == []

    async def test_a_stored_personal_address_flips_coverage(self, conn):
        """End to end on the write path: what store_result writes is what
        SELECT_TARGETS then reads as coverage."""
        from foray.enrich.contacts import store_result

        cid = await self._make(conn, "stored_1", linkedin_url="https://www.linkedin.com/in/a")
        await store_result(
            conn,
            cid,
            "contactout",
            ProviderResult(emails=[FoundEmail("found@gmail.com", is_work=False)]),
        )
        [record] = await conn.fetch(internal.SELECT_TARGETS, ["stored_1"])
        assert record["has_personal"] is True
        row_source = await conn.fetchval(
            "select source from candidate_emails where candidate_id = $1", cid
        )
        assert row_source == "contactout_personal"
