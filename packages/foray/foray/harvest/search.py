"""The harvest worker: claim a shard, paginate it fully, adaptively split when
the shard is truncated by the 1,000-result cap.

Crash-safety: a shard is claimed with ``FOR UPDATE SKIP LOCKED`` and a lease;
progress is checkpointed after every page, so a SIGKILL loses at most one page,
and the lease lets another worker reclaim a shard abandoned mid-run. Logins are
inserted with ``ON CONFLICT (login) DO NOTHING`` so restarts never duplicate.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import settings
from ..db import _asyncpg_dsn
from ..log import Heartbeat, get_logger
from ..ratelimit import RateLimiter
from .shards import compose_query

log = get_logger("harvest.search")

GITHUB_API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 10  # 1,000-result cap / 100 per page
LEASE = "15 minutes"

# --------------------------------------------------------------- date splitting --


def parse_range(created_range: str) -> tuple[date, date]:
    start, end = created_range.split("..")
    return date.fromisoformat(start), date.fromisoformat(end)


def format_range(start: date, end: date) -> str:
    return f"{start.isoformat()}..{end.isoformat()}"


def split_range(created_range: str) -> list[str] | None:
    """Halve a created-date range. Return None at month granularity (<=31 days),
    which signals "accept the truncation" per the spec."""
    start, end = parse_range(created_range)
    span = (end - start).days
    if span <= 31:
        return None
    mid = start + timedelta(days=span // 2)
    return [format_range(start, mid), format_range(mid + timedelta(days=1), end)]


# ------------------------------------------------------------------ GitHub client --


class ServerError(Exception):
    """A GitHub 5xx; retried with exponential backoff via tenacity."""


class GitHubSearch:
    def __init__(self, token: str, limiter: RateLimiter) -> None:
        self._limiter = limiter
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "foray-harvester",
            },
            timeout=30.0,
        )
        self.request_count = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, ServerError)),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _get(self, query: str, page: int) -> httpx.Response:
        await self._limiter.wait()
        self.request_count += 1
        resp = await self._client.get(
            "/search/users",
            params={"q": query, "per_page": PER_PAGE, "page": page},
        )
        if resp.status_code >= 500:
            raise ServerError(f"github {resp.status_code}")
        return resp

    async def search_users(self, query: str, page: int) -> dict[str, Any]:
        while True:
            resp = await self._get(query, page)
            if resp.status_code == 403:
                retry_after = resp.headers.get("retry-after")
                remaining = resp.headers.get("x-ratelimit-remaining")
                if retry_after is not None:
                    delay = float(retry_after) + random.uniform(0, 3)
                    log.info("secondary_rate_limit", retry_after=retry_after, sleep=delay)
                    await asyncio.sleep(delay)
                    continue
                if remaining == "0":
                    reset = int(resp.headers.get("x-ratelimit-reset", "0"))
                    delay = max(0.0, reset - time.time()) + 5 + random.uniform(0, 5)
                    log.info("primary_rate_limit", reset=reset, sleep=round(delay, 1))
                    await asyncio.sleep(delay)
                    continue
            resp.raise_for_status()
            return resp.json()


# ------------------------------------------------------------------- DB helpers --

CLAIM_SQL = f"""
update harvest_shards
set status = 'running',
    lease_expires_at = now() + interval '{LEASE}',
    updated_at = now()
where id = (
    select id from harvest_shards
    where status = 'pending'
       or (status = 'running' and lease_expires_at < now())
    order by id
    for update skip locked
    limit 1
)
returning id, location, language, followers, created_range, query
"""


async def claim_shard(conn: asyncpg.Connection) -> asyncpg.Record | None:
    return await conn.fetchrow(CLAIM_SQL)


async def insert_logins(
    conn: asyncpg.Connection, items: list[dict[str, Any]], shard_id: int
) -> int:
    logins, ids, shards = [], [], []
    for item in items:
        login = item.get("login")
        if not login:
            continue
        logins.append(login)
        ids.append(item.get("id"))
        shards.append(str(shard_id))
    if not logins:
        return 0
    rows = await conn.fetch(
        """insert into candidates (login, github_id, discovery_shard)
           select * from unnest($1::text[], $2::bigint[], $3::text[])
           on conflict (login) do nothing
           returning id""",
        logins,
        ids,
        shards,
    )
    return len(rows)


async def _paginate(
    conn: asyncpg.Connection,
    gh: GitHubSearch,
    shard: asyncpg.Record,
    first_page: dict[str, Any],
    pages: int,
) -> int:
    found = await insert_logins(conn, first_page.get("items", []), shard["id"])
    await conn.execute(
        "update harvest_shards set pages_done = 1, found = $1, updated_at = now() where id = $2",
        found,
        shard["id"],
    )
    for page in range(2, pages + 1):
        data = await gh.search_users(shard["query"], page)
        items = data.get("items", [])
        if not items:
            break
        found += await insert_logins(conn, items, shard["id"])
        await conn.execute(
            "update harvest_shards set pages_done = $1, found = $2, updated_at = now() where id = $3",
            page,
            found,
            shard["id"],
        )
    return found


async def _enqueue_children(conn: asyncpg.Connection, shard: asyncpg.Record, ranges: list[str]) -> None:
    for created_range in ranges:
        await conn.execute(
            """insert into harvest_shards
                   (location, language, followers, created_range, query, parent_id)
               values ($1, $2, $3, $4, $5, $6)
               on conflict (location, language, followers, created_range) do nothing""",
            shard["location"],
            shard["language"],
            shard["followers"],
            created_range,
            compose_query(shard["location"], shard["language"], shard["followers"], created_range),
            shard["id"],
        )


async def process_shard(conn: asyncpg.Connection, gh: GitHubSearch, shard: asyncpg.Record) -> None:
    data = await gh.search_users(shard["query"], 1)
    total = int(data.get("total_count", 0))
    await conn.execute(
        "update harvest_shards set total_count = $1, updated_at = now() where id = $2",
        total,
        shard["id"],
    )

    if total == 0:
        await conn.execute(
            "update harvest_shards set status = 'done', updated_at = now() where id = $1",
            shard["id"],
        )
        return

    if total > 1000:
        children = split_range(shard["created_range"])
        if children is None:
            # Month granularity and still truncated: accept it, take the first 1,000.
            await _paginate(conn, gh, shard, data, MAX_PAGES)
            await conn.execute(
                "update harvest_shards set status = 'truncated', error = 'accepted', "
                "updated_at = now() where id = $1",
                shard["id"],
            )
            log.info("shard.truncated", shard_id=shard["id"], total=total, reason="accepted")
        else:
            await _enqueue_children(conn, shard, children)
            await conn.execute(
                "update harvest_shards set status = 'truncated', error = 'split', "
                "updated_at = now() where id = $1",
                shard["id"],
            )
            log.info("shard.split", shard_id=shard["id"], total=total, children=children)
        return

    pages = min(MAX_PAGES, (total + PER_PAGE - 1) // PER_PAGE)
    found = await _paginate(conn, gh, shard, data, pages)
    await conn.execute(
        "update harvest_shards set status = 'done', updated_at = now() where id = $1",
        shard["id"],
    )
    log.info("shard.done", shard_id=shard["id"], total=total, found=found)


# ------------------------------------------------------------------------ report --


async def _report(conn: asyncpg.Connection) -> dict[str, int]:
    total_shards = await conn.fetchval("select count(*) from harvest_shards")
    pending = await conn.fetchval("select count(*) from harvest_shards where status = 'pending'")
    truncated = await conn.fetchval("select count(*) from harvest_shards where status = 'truncated'")
    logins = await conn.fetchval("select count(*) from candidates")
    return {
        "total_shards": total_shards,
        "pending_shards": pending,
        "truncated_shards": truncated,
        "distinct_logins": logins,
    }


# -------------------------------------------------------------------------- main --


async def _dry_run(limit: int) -> int:
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        rows = await conn.fetch(
            "select query from harvest_shards where status = 'pending' order by id limit $1",
            max(1, limit),
        )
        for row in rows:
            print(row["query"])
        print(f"[dry-run] {len(rows)} pending shard queries (no API calls made)")
    finally:
        await conn.close()
    return 0


async def amain(args: argparse.Namespace) -> int:
    if args.dry_run:
        return await _dry_run(args.limit or 20)

    settings.require("github_token")
    limiter = RateLimiter(settings.github_rpm)
    gh = GitHubSearch(settings.github_token, limiter)
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    processed = 0
    try:
        with Heartbeat(
            log,
            lambda: {"processed": processed, "requests": gh.request_count},
        ):
            while args.limit == 0 or processed < args.limit:
                shard = await claim_shard(conn)
                if shard is None:
                    log.info("no_pending_shards")
                    break
                try:
                    await process_shard(conn, gh, shard)
                except Exception as exc:  # noqa: BLE001 - isolate one bad shard
                    await conn.execute(
                        "update harvest_shards set status = 'pending', error = $1, "
                        "lease_expires_at = null, updated_at = now() where id = $2",
                        str(exc)[:500],
                        shard["id"],
                    )
                    log.error("shard.error", shard_id=shard["id"], error=str(exc))
                processed += 1
        report = await _report(conn)
        log.info("harvest.report", processed=processed, **report)
        print(
            f"processed {processed} shards | "
            f"shards={report['total_shards']} pending={report['pending_shards']} "
            f"truncated={report['truncated_shards']} distinct_logins={report['distinct_logins']}"
        )
    finally:
        await gh.aclose()
        await conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Harvest GitHub logins by claiming shards.")
    ap.add_argument("--limit", type=int, default=0, help="process at most N shards (0 = all pending)")
    ap.add_argument("--dry-run", action="store_true", help="print pending queries, call no API")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
