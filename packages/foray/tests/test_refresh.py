from __future__ import annotations

import asyncpg

from foray.config import settings
from foray.db import _asyncpg_dsn
from foray.refresh.common import RefreshRun
from tests.dbutil import requires_db


@requires_db
async def test_refresh_run_logs_start_and_end():
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    tr = conn.transaction()
    await tr.start()
    try:
        async with RefreshRun(conn, "unit_test") as run:
            run.rows = 7
            run.api_calls = 3
        row = await conn.fetchrow(
            "select job, rows_touched, api_calls, ended_at from refresh_runs where id = $1", run.id
        )
        assert row["job"] == "unit_test"
        assert row["rows_touched"] == 7
        assert row["api_calls"] == 3
        assert row["ended_at"] is not None
    finally:
        await tr.rollback()
        await conn.close()


@requires_db
async def test_active_candidates_view_exists():
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        kind = await conn.fetchval(
            "select relkind::text from pg_class where relname = 'active_candidates'"
        )
        assert kind == "m"  # materialized view
    finally:
        await conn.close()
