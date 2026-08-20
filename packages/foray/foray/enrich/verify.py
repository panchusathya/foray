"""Free corroboration scoring for ``candidate_emails``.

``verified`` is reserved for a provider that asserts deliverability. This
module is the free substitute: a pure ``reach_score`` over signals we can
observe without SMTP probing, plus a Gravatar existence probe and a batch
runner that fills unscored / stale rows.

A Gravatar miss must not suppress an address. Plenty of real developers have
no Gravatar (a 404 is absence of an avatar, not absence of a mailbox). A
non-200/404 is ``unknown``, never ``miss``.

``reach_score`` is a send-floor, not a re-rank. Search still orders by
``is_work asc, confidence desc``; the sequencer refuses to send below
``FORAY_MIN_REACH_SCORE``. Unscored rows (null) are never blocked.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx

from ..db import close_pool, get_pool
from ..log import Heartbeat, get_logger
from ..ratelimit import RateLimiter
from .employer import is_work_domain

log = get_logger("enrich.verify")

GravatarState = Literal["hit", "miss", "unknown"]

GRAVATAR_URL = "https://www.gravatar.com/avatar/{hash}"
# Polite; Gravatar is a free existence check, not a paid API.
GRAVATAR_RPM = 60


@dataclass(frozen=True)
class ReachInputs:
    source_count: int = 1
    occurrences: int = 1
    last_evidence_at: datetime | None = None
    gravatar_state: GravatarState = "unknown"
    mx_present: bool | None = None
    is_freemail: bool = True
    now: datetime | None = None


def reach_score(row: ReachInputs) -> int:
    """0-100. Pure: no I/O. A Gravatar miss adds nothing and subtracts nothing."""
    score = 20  # passed placeholder + usable-commit gates to get here

    if row.source_count >= 3:
        score += 28
    elif row.source_count >= 2:
        score += 18

    score += min(20, max(0, row.occurrences) * 2)

    last = row.last_evidence_at
    if last is not None:
        now = row.now or datetime.now(UTC)
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        age_days = max(0, (now - last).days)
        if age_days <= 365:
            score += 15
        elif age_days <= 730:
            score += 10
        elif age_days <= 1825:
            score += 5

    # Corroboration only. miss/unknown do not degrade the score.
    if row.gravatar_state == "hit":
        score += 15

    if row.mx_present is True:
        score += 8
    # mx_present False/None: no penalty. An MX miss is not "this mailbox is dead".

    if not row.is_freemail:
        score += 10

    return max(0, min(100, score))


def gravatar_hash(email: str) -> str:
    return hashlib.md5(
        email.strip().lower().encode("utf-8"), usedforsecurity=False
    ).hexdigest()


def interpret_gravatar_status(status_code: int) -> GravatarState:
    """Map an HTTP status to a cached verdict. Non-200/404 is unknown."""
    if status_code == 200:
        return "hit"
    if status_code == 404:
        return "miss"
    return "unknown"


async def probe_gravatar(client: httpx.AsyncClient, email: str) -> GravatarState:
    url = GRAVATAR_URL.format(hash=gravatar_hash(email))
    try:
        resp = await client.head(url, params={"d": "404"})
    except httpx.HTTPError:
        return "unknown"
    return interpret_gravatar_status(resp.status_code)


def inputs_from_row(row, *, gravatar_state: GravatarState, now: datetime) -> ReachInputs:
    return ReachInputs(
        source_count=int(row["source_count"] or 1),
        occurrences=int(row["occurrences"] or 1),
        last_evidence_at=row["last_evidence_at"] or row["last_seen_at"],
        gravatar_state=gravatar_state,
        mx_present=None,
        is_freemail=not is_work_domain(row["email"]),
        now=now,
    )


async def score_unscored(
    conn,
    *,
    limit: int = 0,
    concurrency: int = 8,
    dry_run: bool = False,
    stale_days: int = 30,
) -> int:
    """Fill reach_score on unscored / stale-Gravatar rows. No GitHub quota."""
    rows = await conn.fetch(
        f"""
        select e.id, e.email, e.source_count, e.occurrences,
               e.last_evidence_at, e.last_seen_at, e.gravatar_state, e.gravatar_checked_at
        from candidate_emails e
        where e.source <> 'guess'
          and (e.reach_score is null
               or e.gravatar_checked_at is null
               or e.gravatar_checked_at < now() - make_interval(days => $1))
        order by e.id
        {"limit $2" if limit else ""}
        """,
        stale_days,
        *([limit] if limit else []),
    )
    if dry_run:
        print(f"verify would score {len(rows)} addresses (dry run)")
        return 0

    limiter = RateLimiter(GRAVATAR_RPM)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    scored = 0
    now = datetime.now(UTC)

    async def one(client: httpx.AsyncClient, row) -> tuple:
        async with semaphore:
            cached = row["gravatar_state"]
            checked = row["gravatar_checked_at"]
            fresh = (
                cached in {"hit", "miss", "unknown"}
                and checked is not None
                and (now - checked) < timedelta(days=stale_days)
            )
            if fresh:
                state: GravatarState = cached
            else:
                await limiter.wait()
                state = await probe_gravatar(client, row["email"])
            return row, state

    async with httpx.AsyncClient(
        timeout=10.0,
        headers={"User-Agent": "foray-verify"},
        follow_redirects=True,
    ) as client:
        with Heartbeat(log, lambda: {"scored": scored}):
            tasks = [asyncio.ensure_future(one(client, r)) for r in rows]
            for coro in asyncio.as_completed(tasks):
                row, state = await coro
                score = reach_score(inputs_from_row(row, gravatar_state=state, now=now))
                await conn.execute(
                    """
                    update candidate_emails
                       set gravatar_state = $2,
                           gravatar_checked_at = now(),
                           reach_score = $3
                     where id = $1
                    """,
                    row["id"],
                    state,
                    score,
                )
                scored += 1

    log.info("verify.done", scored=scored)
    print(f"verify: scored {scored} addresses")
    return scored


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await score_unscored(
                conn,
                limit=args.limit,
                concurrency=args.concurrency,
                dry_run=args.dry_run,
            )
    finally:
        await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Score candidate emails with free corroboration.")
    ap.add_argument("--limit", type=int, default=0, help="max addresses (0 = all)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
