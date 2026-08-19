from __future__ import annotations

from tests.dbutil import requires_db
from foray.db import close_pool, fetchval, get_pool


@requires_db
async def test_pool_opens_and_selects_one():
    pool = await get_pool()
    assert pool is not None
    value = await fetchval("select 1")
    assert value == 1
    await close_pool()


@requires_db
async def test_core_tables_exist():
    expected = {
        "candidates",
        "harvest_shards",
        "raw_profiles",
        "candidate_signals",
        "archetypes",
        "archetype_scores",
        "enrich_errors",
        "scoring_errors",
        "refresh_runs",
        "candidate_emails",
        "shortlist_events",
        "query_parse_cache",
    }
    count = await fetchval(
        "select count(*) from information_schema.tables "
        "where table_schema = 'public' and table_name = any($1::text[])",
        list(expected),
    )
    assert count == len(expected)
    await close_pool()
