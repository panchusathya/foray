"""Database access: an asyncpg pool for services, plus a sync psycopg
connection for scripts. No ORM (by design).
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None

# Generous for a healthy close, short enough that a deadlock fails a CI job
# in seconds instead of sitting on a runner until the 6-hour ceiling.
CLOSE_TIMEOUT_SECONDS = 15.0


def _asyncpg_dsn(url: str) -> str:
    # asyncpg wants a bare postgresql:// DSN, without a SQLAlchemy-style +driver.
    return (
        url.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg://", "postgresql://")
        .replace("postgres://", "postgresql://")
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=_asyncpg_dsn(settings.database_url),
            min_size=1,
            max_size=10,
            command_timeout=60,
        )
    return _pool


async def close_pool() -> None:
    """Close the pool, but never hang the process doing it.

    Pool.close() waits for every acquired connection to be released, so a
    caller that closes while still holding one deadlocks forever -- and a
    half-open socket to a wedged database can stall the graceful close just as
    well. Both used to burn CI runs for hours. Bound it and fall back to a
    forced terminate, which is what asyncpg's own "taking over 60 seconds"
    warning tells you to do.
    """
    global _pool
    if _pool is None:
        return
    pool, _pool = _pool, None
    try:
        await asyncio.wait_for(pool.close(), timeout=CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        pool.terminate()
        raise SystemExit(
            "database pool did not close within "
            f"{CLOSE_TIMEOUT_SECONDS}s and was terminated -- a caller is very "
            "likely awaiting close_pool() while still holding a connection"
        ) from None


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)


def sync_connect():
    """A plain psycopg connection for one-off scripts (migrations, backfills)."""
    import psycopg

    return psycopg.connect(_asyncpg_dsn(settings.database_url))
