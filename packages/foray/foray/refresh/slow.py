"""Quarterly full-pool refresh: re-derive signals, re-embed changed profiles, and
rescore stale archetypes. Rescoring reuses the Phase 4 --stale-only path (only
rows whose rubric_hash no longer matches), not a full rescore.

--stale-only is a scope, not a budget. One edit to archetypes.yaml's shared
rubric tail invalidates every rubric_hash in the pool, and this job runs
unattended: it passes --yes, which is exactly the flag that suppresses the
"projected $X exceeds the threshold, confirm" prompt. So the spend guard here
cannot be a prompt -- it is a hard call ceiling, taken from what is left of
the month's cap, and the calls this job makes are logged as `slow.score` so
that the same cap sees them next time.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ..config import settings
from ..db import close_pool, get_pool
from ..log import get_logger
from .common import RefreshRun, calls_left_this_month, month_spend, scoring_calls_since

log = get_logger("refresh.slow")


async def _run_step(argv: list[str]) -> int:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", *argv)
    return await proc.wait()


def score_args(*, calls_left: int) -> list[str] | None:
    """The rescore step's argv, or None when the month's budget is spent.

    ``--yes`` only ever travels with ``--max-calls``: unattended confirmation
    and no ceiling is the combination that turns a rubric edit into tens of
    thousands of unwatched calls.
    """
    if not settings.openrouter_api_key:
        return ["foray.score.run", "--stale-only", "--offline"]
    if calls_left <= 0:
        return None
    return [
        "foray.score.run",
        "--stale-only",
        "--yes",
        "--max-calls",
        str(calls_left),
    ]


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        stale = await conn.fetchval(
            "select count(*) from archetype_scores a join archetypes t on t.id = a.archetype_id "
            "where a.rubric_hash <> t.rubric_hash"
        )
        unembedded = await conn.fetchval(
            "select count(*) from candidates c join candidate_signals s on s.candidate_id = c.id "
            "where c.embedding is null"
        )
        spend = await month_spend(conn)
        calls_left = await calls_left_this_month(conn)
        # The server's clock: what scoring_calls_since compares against.
        started_at = await conn.fetchval("select now()")
    scoring = score_args(calls_left=calls_left)
    if args.dry_run:
        print(f"[dry-run] slow refresh: {stale} stale scores, {unembedded} unembedded candidates")
        print(f"  month-to-date scoring spend ${spend:.2f} / "
              f"${settings.monthly_spend_cap_usd:.0f} cap, {calls_left} calls left")
        print("  steps: enrich.derive -> embed.run -> "
              + (" ".join(scoring) if scoring else "score.run SKIPPED (month cap reached)"))
        await close_pool()
        return 0

    await close_pool()  # sub-steps manage their own pools

    steps = [
        ["foray.enrich.derive"],
        ["foray.embed.run"],
    ]
    if scoring is None:
        log.info("slow.score_skipped", reason=f"month spend ${spend:.2f} >= cap")
    else:
        steps.append(scoring)
    errors = 0
    for step in steps:
        code = await _run_step(step)
        if code != 0:
            errors += 1
            log.error("slow.step_failed", step=step, code=code)

    pool = await get_pool()
    async with pool.acquire() as conn:
        if scoring is not None:
            # Logged separately from the `slow` row and under a job name the
            # monthly cap sums, so this job's spend is visible to the next
            # nightly instead of being invisible until the invoice.
            async with RefreshRun(conn, "slow.score") as score_run:
                score_run.rows = await scoring_calls_since(conn, started_at)
        async with RefreshRun(conn, "slow") as run:
            run.rows = stale + unembedded
            run.errors = errors
            run.notes = f"stale={stale} unembedded={unembedded}"
    await close_pool()
    print(f"slow refresh complete ({errors} step error(s))")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Quarterly full-pool refresh (reuses --stale-only).")
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
