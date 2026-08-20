"""Shared Postgres reachability helpers for Foray tests.

DB-backed tests skip themselves when Postgres is not reachable so offline unit
tests always run. CI sets ``FORAY_DATABASE_URL`` and starts pgvector, so the
same tests execute there.
"""
from __future__ import annotations

import asyncio

import pytest

from foray.config import settings
from foray.db import _asyncpg_dsn


def postgres_available() -> bool:
    async def _check() -> bool:
        try:
            import asyncpg

            conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
            await conn.close()
            return True
        except Exception:  # noqa: BLE001 - any connection failure means unavailable
            return False

    try:
        return asyncio.run(_check())
    except Exception:  # noqa: BLE001 - event-loop setup can also make it unavailable
        return False


def scored_db_available() -> bool:
    async def _check() -> bool:
        try:
            import asyncpg

            conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
            try:
                n = await conn.fetchval("select count(*) from archetype_scores")
            finally:
                await conn.close()
            return bool(n)
        except Exception:  # noqa: BLE001 - schema/connection failures mean unavailable
            return False

    try:
        return asyncio.run(_check())
    except Exception:  # noqa: BLE001 - event-loop setup can also make it unavailable
        return False


requires_db = pytest.mark.skipif(not postgres_available(), reason="Postgres not reachable")
requires_scored_db = pytest.mark.skipif(
    not scored_db_available(), reason="Postgres not reachable or no archetype_scores"
)
