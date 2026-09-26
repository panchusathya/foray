from __future__ import annotations

import asyncpg

from foray.config import settings
from foray.db import _asyncpg_dsn
from foray.harvest.search import (
    claim_shard,
    insert_logins,
    parse_range,
    split_range,
)
from tests.dbutil import requires_db

# ------------------------------------------------------------------ offline logic --


def test_split_year_halves():
    parts = split_range("2012-01-01..2012-12-31")
    assert parts is not None and len(parts) == 2
    (a0, a1) = parse_range(parts[0])
    (b0, b1) = parse_range(parts[1])
    assert a0.isoformat() == "2012-01-01"
    assert b1.isoformat() == "2012-12-31"
    # contiguous, non-overlapping
    assert (b0 - a1).days == 1


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
