from __future__ import annotations

import asyncio

import asyncpg
import pytest

from foray.config import settings
from foray.db import _asyncpg_dsn, close_pool, fetchval, get_pool


def _db_reachable() -> bool:
    async def _check() -> bool:
        try:
            conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
            await conn.close()
            return True
        except Exception:
            return False

    try:
        return asyncio.run(_check())
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


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
    }
    count = await fetchval(
        "select count(*) from information_schema.tables "
        "where table_schema = 'public' and table_name = any($1::text[])",
        list(expected),
    )
    assert count == len(expected)
    await close_pool()
