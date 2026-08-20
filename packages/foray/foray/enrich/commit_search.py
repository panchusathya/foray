"""Whole-graph commit-author email mining via the GitHub Search API.

Own-repo mining (``enrich.emails.mine_commit_emails``) hits
``/repos/{login}/{repo}/commits?author={login}`` for the candidate's top
repos. That misses anyone who contributes without owning, and misses older
addresses. This module makes one Search API call per candidate:

    GET /search/commits?q=author:{login}&per_page=100&sort=author-date

and keeps only items whose ``item.author.login`` matches the candidate.
Attribution is then GitHub's, not a heuristic -- the same property the
own-repo miner relies on.

Search sits on the 30/min bucket (``FORAY_GITHUB_RPM``), separate from the
core REST bucket own-repo mining uses, so the two can run in the same night
without sharing quota. Harvest also uses the search bucket; nightly runs
them sequentially, never concurrently.

Source stays ``commit`` at confidence 75. Same evidence class as today, so
no ladder change and no migration on the source check constraint.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

import httpx

from ..config import settings
from ..db import close_pool, get_pool
from ..log import Heartbeat, get_logger
from ..ratelimit import RateLimiter
from ._http import request_with_retry
from .contacts import is_placeholder
from .emails import UPSERT_EMAIL, is_usable_commit_email
from .employer import is_work_domain

log = get_logger("enrich.commit_search")

GITHUB_COMMIT_SEARCH = "https://api.github.com/search/commits"

# Search commits used to require the cloak preview Accept. The 2022-11-28
# API version made it generally available; sending both keeps older tokens
# working and is what GitHub still documents.
_SEARCH_HEADERS = {
    "Accept": "application/vnd.github.cloak-preview+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "foray-enricher",
}


def extract_attributed_commit_emails(
    items: list[dict], login: str
) -> dict[str, dict]:
    """Collapse Search API items into {email: {occurrences, last_date}}.

    Items whose ``author.login`` does not case-insensitively equal ``login``
    are dropped -- GitHub did not attribute them to this account. ``author``
    is null when the git email is not linked to any GitHub user; those are
    dropped for the same reason.
    """
    want = (login or "").lower()
    found: dict[str, dict] = {}
    for item in items:
        author = item.get("author") or {}
        attributed = (author.get("login") or "").lower()
        if not want or attributed != want:
            continue
        commit_author = (item.get("commit") or {}).get("author") or {}
        email = (commit_author.get("email") or "").strip().lower()
        if not is_usable_commit_email(email):
            continue
        date_raw = commit_author.get("date") or ""
        entry = found.setdefault(email, {"occurrences": 0, "last_date": date_raw})
        entry["occurrences"] += 1
        if date_raw and date_raw > (entry["last_date"] or ""):
            entry["last_date"] = date_raw
    return found


def _evidence(login: str, occurrences: int, last_date: str) -> str:
    when = last_date[:10] if last_date else "unknown date"
    return f"commit search author:{login} ({occurrences} commit(s), last {when})"


async def _search_page(
    client: httpx.AsyncClient, limiter: RateLimiter, query: str, page: int
) -> tuple[list[dict], int]:
    await limiter.wait()
    resp = await request_with_retry(
        client,
        "GET",
        GITHUB_COMMIT_SEARCH,
        log=log,
        params={
            "q": query,
            "per_page": 100,
            "page": page,
            "sort": "author-date",
            "order": "desc",
        },
    )
    body = resp.json() if resp.content else {}
    if not isinstance(body, dict):
        return [], 0
    items = body.get("items") or []
    total = int(body.get("total_count") or 0)
    return items if isinstance(items, list) else [], total


async def search_commit_emails_for_login(
    client: httpx.AsyncClient, limiter: RateLimiter, login: str
) -> dict[str, dict]:
    """One (optionally two) Search calls. Second page only when page 1 is a
    single attributed address and GitHub says there are more commits."""
    query = f"author:{login}"
    items, total = await _search_page(client, limiter, query, 1)
    found = extract_attributed_commit_emails(items, login)
    if len(found) == 1 and len(items) == 100 and total > 100:
        more, _ = await _search_page(client, limiter, query, 2)
        extra = extract_attributed_commit_emails(more, login)
        for email, agg in extra.items():
            cur = found.setdefault(email, {"occurrences": 0, "last_date": ""})
            cur["occurrences"] += agg["occurrences"]
            if agg["last_date"] > (cur["last_date"] or ""):
                cur["last_date"] = agg["last_date"]
    return found


async def mine_commit_search(
    conn,
    *,
    limit: int = 0,
    concurrency: int = 2,
    dry_run: bool = False,
) -> int:
    """Mine commit-author emails via Search API. Search quota (30/min)."""
    settings.require("github_token")
    rows = await conn.fetch(
        f"""
        select c.id, c.login
        from candidates c
        where c.source in ('github', 'both')
          and c.merged_into is null
          and (c.commit_search_checked_at is null
               or c.commit_search_checked_at < now() - interval '180 days')
        order by c.id
        {"limit $1" if limit else ""}
        """,
        *([limit] if limit else []),
    )
    if dry_run:
        print(f"commit search would check {len(rows)} candidates (dry run)")
        return 0

    limiter = RateLimiter(settings.github_rpm)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    stored = 0
    checked = 0

    async def fetch_one(client: httpx.AsyncClient, row) -> tuple:
        async with semaphore:
            try:
                found = await search_commit_emails_for_login(client, limiter, row["login"])
                return row, found, None
            except Exception as exc:  # noqa: BLE001 - recorded per-row, never fatal
                return row, {}, exc

    async def _persist(row, found: dict, exc) -> None:
        nonlocal checked, stored
        checked += 1
        if exc is not None:
            await conn.execute(
                "insert into enrich_errors (candidate_id, login, stage, error) "
                "values ($1, $2, 'commit_search', $3)",
                row["id"],
                row["login"],
                str(exc)[:500],
            )
            return
        for email, agg in found.items():
            if is_placeholder(email):
                continue
            occurrences = int(agg["occurrences"])
            last_date = agg.get("last_date") or ""
            await conn.execute(
                UPSERT_EMAIL,
                row["id"],
                email,
                "commit",
                is_work_domain(email),
                75,
                False,
                _evidence(row["login"], occurrences, last_date),
                occurrences,
            )
            parsed = _parse_iso(last_date)
            if parsed is not None:
                await conn.execute(
                    "update candidate_emails set last_evidence_at = "
                    "greatest(coalesce(last_evidence_at, $3), $3) "
                    "where candidate_id = $1 and email = $2",
                    row["id"],
                    email,
                    parsed,
                )
            stored += 1
        await conn.execute(
            "update candidates set commit_search_checked_at = now() where id = $1",
            row["id"],
        )

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={
            **_SEARCH_HEADERS,
            "Authorization": f"Bearer {settings.github_token}",
        },
    ) as client:
        with Heartbeat(log, lambda: {"checked": checked, "stored": stored}):
            tasks = [asyncio.ensure_future(fetch_one(client, r)) for r in rows]
            for coro in asyncio.as_completed(tasks):
                row, found, exc = await coro
                await _persist(row, found, exc)

    log.info("commit_search.done", checked=checked, stored=stored)
    print(f"commit search: checked {checked} candidates, stored {stored} addresses")
    return stored


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await mine_commit_search(
                conn,
                limit=args.limit,
                concurrency=args.concurrency,
                dry_run=args.dry_run,
            )
    finally:
        await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mine commit-author emails via GitHub commit search."
    )
    ap.add_argument("--limit", type=int, default=0, help="max candidates (0 = all)")
    ap.add_argument("--concurrency", type=int, default=2, help="parallel search fetches")
    ap.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
