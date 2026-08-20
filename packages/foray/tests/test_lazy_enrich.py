from __future__ import annotations

import pytest

from foray.enrich.freshness import freshness_check
from foray.enrich.linkedin import find_linkedin, resolve_linkedin


class RecordingFetcher:
    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []

    async def get_text(self, url: str) -> str | None:
        self.calls.append(url)
        return self.pages.get(url)


def test_find_linkedin_extracts_url():
    assert find_linkedin("reach me at https://www.linkedin.com/in/ada-lovelace/ ok") == (
        "https://www.linkedin.com/in/ada-lovelace"
    )
    assert find_linkedin("no link here") is None


async def test_waterfall_stops_at_step_1_and_skips_network_and_apollo():
    apollo_called = False

    async def apollo(_node):
        nonlocal apollo_called
        apollo_called = True
        return "https://linkedin.com/in/should-not-happen"

    fetcher = RecordingFetcher()
    node = {"login": "ada", "bio": "ML. https://linkedin.com/in/ada", "websiteUrl": ""}
    url, source = await resolve_linkedin(
        node, fetcher=fetcher, allow_apollo=True, apollo_reason="shortlist", apollo=apollo
    )
    assert url == "https://linkedin.com/in/ada"
    assert source == "bio"
    assert fetcher.calls == []  # step 1 hit -> no README/website fetch
    assert apollo_called is False  # and no Apollo credit spent


async def test_waterfall_falls_through_to_readme():
    fetcher = RecordingFetcher(
        {"https://raw.githubusercontent.com/ada/ada/main/README.md": "see https://linkedin.com/in/ada-readme"}
    )
    node = {"login": "ada", "bio": "", "websiteUrl": ""}
    url, source = await resolve_linkedin(node, fetcher=fetcher)
    assert url == "https://linkedin.com/in/ada-readme"
    assert source == "readme"


async def test_apollo_requires_reason():
    fetcher = RecordingFetcher()
    node = {"login": "ghost", "bio": "", "websiteUrl": ""}
    with pytest.raises(ValueError):
        await resolve_linkedin(node, fetcher=fetcher, allow_apollo=True, apollo_reason=None)


async def test_apollo_not_called_without_allow_flag():
    called = False

    async def apollo(_node):
        nonlocal called
        called = True
        return "x"

    node = {"login": "ghost", "bio": "", "websiteUrl": ""}
    url, source = await resolve_linkedin(
        node, fetcher=RecordingFetcher(), allow_apollo=False, apollo=apollo
    )
    assert url is None and source is None
    assert called is False


# --------------------------------------------------------------- freshness --

import asyncpg

from foray.config import settings
from foray.db import _asyncpg_dsn
from tests.dbutil import requires_db


@requires_db
async def test_freshness_detects_employer_change_and_badges():
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    tr = conn.transaction()
    await tr.start()
    try:
        cid = await conn.fetchval(
            "insert into candidates (login) values ('fresh_test') returning id"
        )
        await conn.execute(
            "insert into candidate_signals (candidate_id, employer, employer_source) "
            "values ($1, 'OldCo', 'company')",
            cid,
        )

        async def fake_fetch(_login):
            return {"login": "fresh_test", "company": "@NewCo", "organizations": {"nodes": []}}

        result = await freshness_check(conn, "fresh_test", fetch_node=fake_fetch)
        assert result["changed"] is True
        assert result["previous"] == "OldCo"
        assert result["current"] == "NewCo"
        assert "employer updated just now" in result["badge"]
        assert "previously OldCo" in result["badge"]

        # No change on a second identical fetch.
        result2 = await freshness_check(conn, "fresh_test", fetch_node=fake_fetch)
        assert result2["changed"] is False
    finally:
        await tr.rollback()
        await conn.close()
