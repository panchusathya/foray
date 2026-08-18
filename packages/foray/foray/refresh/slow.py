"""Quarterly full-pool refresh: re-derive signals, re-embed changed profiles, and
rescore stale archetypes. Rescoring reuses the Phase 4 --stale-only path (only
rows whose rubric_hash no longer matches), not a full rescore.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ..config import settings
from ..db import close_pool, get_pool
from ..log import get_logger
from .common import RefreshRun

log = get_logger("refresh.slow")


async def _run_step(argv: list[str]) -> int:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", *argv)
    return await proc.wait()


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
    if args.dry_run:
        print(f"[dry-run] slow refresh: {stale} stale scores, {unembedded} unembedded candidates")
        print("  steps: enrich.derive -> embed.run -> score.run --stale-only")
        await close_pool()
        return 0

    await close_pool()  # sub-steps manage their own pools

    score_args = ["foray.score.run", "--stale-only"]
    score_args.append("--offline" if not settings.anthropic_api_key else "--yes")

    steps = [
        ["foray.enrich.derive"],
        ["foray.embed.run"],
        score_args,
    ]
    errors = 0
    for step in steps:
        code = await _run_step(step)
        if code != 0:
            errors += 1
            log.error("slow.step_failed", step=step, code=code)

    pool = await get_pool()
    async with pool.acquire() as conn:
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
