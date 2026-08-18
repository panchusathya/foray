from __future__ import annotations

import asyncio

import asyncpg
import pytest

from foray.config import settings
from foray.db import _asyncpg_dsn
from foray.harvest.shards import (
    FOLLOWER_BUCKETS,
    LANGUAGES,
    YEARS,
    compose_query,
    created_range_for_year,
    generate_shards,
    load_metros,
)
from foray.harvest.search import (
    claim_shard,
    insert_logins,
    parse_range,
    split_range,
)


# ------------------------------------------------------------------ offline logic --


def test_dimensions():
    assert len(LANGUAGES) == 12
    assert len(FOLLOWER_BUCKETS) == 6
    assert len(YEARS) == 11
    assert len(load_metros()) == 40


def test_shard_grid_is_31680():
    shards = generate_shards()
    assert len(shards) == 40 * 12 * 6 * 11 == 31680
    # unique on the 4 dimensions
    keys = {(loc, lang, fb, cr) for (loc, lang, fb, cr, _q) in shards}
    assert len(keys) == len(shards)


def test_compose_query_shape():
    q = compose_query("San Francisco", "python", "10..24", created_range_for_year(2012))
    assert 'location:"San Francisco"' in q
    assert "language:python" in q
    assert "followers:10..24" in q
    assert "created:2012-01-01..2012-12-31" in q
    assert "type:user" in q


def test_split_year_halves():
    parts = split_range("2012-01-01..2012-12-31")
    assert parts is not None and len(parts) == 2
    (a0, a1) = parse_range(parts[0])
    (b0, b1) = parse_range(parts[1])
    assert a0.isoformat() == "2012-01-01"
    assert b1.isoformat() == "2012-12-31"
    # contiguous, non-overlapping
    assert (b0 - a1).days == 1


def test_split_stops_at_month_granularity():
    assert split_range("2012-01-01..2012-01-31") is None


def test_split_converges():
    # Repeatedly halving a year must terminate at month granularity.
    ranges = ["2012-01-01..2012-12-31"]
    for _ in range(20):
        nxt: list[str] = []
        for r in ranges:
            children = split_range(r)
            if children is None:
                continue
            nxt.extend(children)
        if not nxt:
            break
        ranges = nxt
    assert all(split_range(r) is None for r in ranges)


# ---------------------------------------------------------------------- DB tests --


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
async def test_login_insert_dedupes():
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    tr = conn.transaction()
    await tr.start()
    try:
        sid = await conn.fetchval(
            "insert into harvest_shards (location, language, followers, created_range, query) "
            "values ('T', 'python', '10..24', 'r', 'q') returning id"
        )
        n1 = await insert_logins(conn, [{"login": "octo_dup", "id": 1}], sid)
        n2 = await insert_logins(conn, [{"login": "octo_dup", "id": 1}], sid)
        assert n1 == 1
        assert n2 == 0  # ON CONFLICT (login) DO NOTHING
    finally:
        await tr.rollback()
        await conn.close()


@requires_db
async def test_claim_uses_skip_locked_and_reclaims_expired_lease():
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    tr = conn.transaction()
    await tr.start()
    try:
        # Isolate: clear within the (rolled-back) transaction, insert controlled rows.
        await conn.execute("delete from harvest_shards")
        pending = await conn.fetchval(
            "insert into harvest_shards (location, language, followers, created_range, query) "
            "values ('A', 'go', '10..24', 'r1', 'q1') returning id"
        )
        # An abandoned 'running' shard whose lease has expired must be reclaimable.
        stale = await conn.fetchval(
            "insert into harvest_shards (location, language, followers, created_range, query, "
            "status, lease_expires_at) values ('B', 'go', '10..24', 'r2', 'q2', 'running', "
            "now() - interval '1 hour') returning id"
        )

        first = await claim_shard(conn)
        assert first is not None and first["id"] == pending
        assert (
            await conn.fetchval("select status from harvest_shards where id = $1", pending)
            == "running"
        )

        second = await claim_shard(conn)
        assert second is not None and second["id"] == stale  # expired lease reclaimed

        third = await claim_shard(conn)
        assert third is None  # nothing left claimable
    finally:
        await tr.rollback()
        await conn.close()
