"""Database access: an asyncpg pool for services, plus a sync psycopg
connection for scripts. No ORM (by design).
"""

from __future__ import annotations

from typing import Any

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None


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
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


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
