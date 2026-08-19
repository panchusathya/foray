"""Bulk LinkedIn backfill over gated, above-floor candidates.

The waterfall (enrich/linkedin.py) has only ever run on-click. This runs it
over the candidates a recruiter would actually contact: having ANY
archetype_scores row means the candidate passed a gate (candidates_to_score
only writes rows for gate matches), so the join IS the gate — no gate SQL is
re-evaluated. Ordered best-score-first so a partial run is still useful.

Never spends an Apollo credit (allow_apollo=False unconditionally). One shared
HttpFetcher per run — the on-click path builds a fresh connection pool per
call, which would be 10k pools here. Outbound rate is capped by
FORAY_LINKEDIN_RPM independent of concurrency.
"""

from __future__ import annotations

import argparse
import asyncio

from ..config import settings
from ..db import close_pool, get_pool
from ..log import Heartbeat, get_logger
from ..ratelimit import RateLimiter
from ..refresh.common import RefreshRun
from .linkedin import HttpFetcher, TextFetcher, resolve_and_store

log = get_logger("enrich.linkedin_bulk")

_TARGETS_SQL = """
select c.id, c.login, max(a.score) as best
from candidates c
join candidate_signals s on s.candidate_id = c.id
join archetype_scores  a on a.candidate_id = c.id
where c.linkedin_url is null
  and ($4 or c.linkedin_checked_at is null
       or c.linkedin_checked_at < now() - make_interval(days => $2))
  and s.last_push_at > now() - make_interval(months => $3)
  and ($5::text is null or a.archetype_id = $5)
group by c.id, c.login
having max(a.score) >= $1
order by best desc, c.id
{limit_clause}
"""


class _PoliteFetcher:
    """Wraps the shared fetcher with the run-wide rate limiter."""

    def __init__(self, inner: TextFetcher, limiter: RateLimiter) -> None:
        self._inner = inner
        self._limiter = limiter

    async def get_text(self, url: str) -> str | None:
        await self._limiter.wait()
        return await self._inner.get_text(url)


async def run_backfill(
    *,
    limit: int = 0,
    concurrency: int = 4,
    archetype: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    pool = await get_pool()
    async with pool.acquire() as run_conn:
        targets = await run_conn.fetch(
            _TARGETS_SQL.format(limit_clause="limit $6" if limit else ""),
            settings.display_floor,
            settings.linkedin_recheck_days,
            settings.activity_floor_months,
            force,
            archetype,
            *([limit] if limit else []),
        )
        if dry_run:
            print(f"linkedin backfill would target {len(targets)} candidates (dry run)")
            return 0

        found = 0
        done = 0
        async with RefreshRun(run_conn, "linkedin_backfill") as run:
            fetcher = HttpFetcher()
            polite = _PoliteFetcher(fetcher, RateLimiter(settings.linkedin_rpm))
            semaphore = asyncio.Semaphore(max(1, concurrency))

            async def one(login: str, cid: int) -> None:
                nonlocal found, done
                async with semaphore:
                    # Each worker takes its own pool connection: asyncpg
                    # connections reject concurrent queries.
                    async with pool.acquire() as conn:
                        try:
                            result = await resolve_and_store(
                                conn, login, fetcher=polite, allow_apollo=False, force=force
                            )
                            if result.get("found"):
                                found += 1
                        except Exception as exc:  # noqa: BLE001 - one dead site must not end the run
                            run.errors += 1
                            await conn.execute(
                                "insert into enrich_errors (candidate_id, login, stage, error) "
                                "values ($1, $2, 'linkedin', $3)",
                                cid,
                                login,
                                str(exc)[:500],
                            )
                    done += 1

            try:
                with Heartbeat(log, lambda: {"done": done, "found": found}):
                    await asyncio.gather(*(one(t["login"], t["id"]) for t in targets))
            finally:
                await fetcher.aclose()
            run.rows = found
            run.api_calls = fetcher.calls
            run.notes = f"targets={len(targets)} concurrency={concurrency}"

        log.info("linkedin_backfill.done", targets=len(targets), found=found)
        print(f"linkedin backfill: {found} resolved of {len(targets)} targets")
        return found


async def amain(args: argparse.Namespace) -> int:
    try:
        await run_backfill(
            limit=args.limit,
            concurrency=args.concurrency,
            archetype=args.archetype,
            dry_run=args.dry_run,
            force=args.force,
        )
    finally:
        await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Bulk LinkedIn resolution for gated candidates.")
    ap.add_argument("--limit", type=int, default=0, help="max candidates (0 = all targets)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--archetype", help="restrict to one archetype id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the negative-cache TTL")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
