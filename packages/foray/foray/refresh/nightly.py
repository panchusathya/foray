"""The unattended nightly growth pipeline: harvest -> enrich -> derive -> embed
-> emails -> score -> refresh, each time-boxed and each isolated from the
others' failures.

Phase isolation is the whole design: every phase reads its work queue from the
database, never from the previous phase's output, so a phase that fails (or is
killed at its deadline) only delays its own backlog. The next night resumes
exactly where this one stopped. Two runs cannot overlap: a session advisory
lock releases on any kind of death, including SIGKILL.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass

from ..config import settings
from ..db import close_pool, get_pool
from ..log import get_logger
from ..status import collect
from .common import RefreshRun

log = get_logger("refresh.nightly")

LOCK_KEY = 0x0F0A  # arbitrary constant for pg_try_advisory_lock


@dataclass(frozen=True)
class Phase:
    name: str
    argv: list[str]
    budget_min: int
    metric: str  # key in status.collect() whose delta = rows_touched


def _phases() -> list[Phase]:
    return [
        Phase("harvest", ["foray.harvest.search", "--limit", str(settings.nightly_shards)], 45, "candidates"),
        Phase("enrich", ["foray.enrich.profiles", "--limit", str(settings.nightly_enrich)], 210, "enriched"),
        Phase("derive", ["foray.enrich.derive", "--stale-only"], 20, "derived"),
        Phase("embed", ["foray.embed.run"], 40, "embedded"),
        Phase("emails", ["foray.enrich.emails", "--source", "commit", "--limit", str(settings.nightly_enrich)], 45, "with_email"),
        # Search-API jobs share the 30/min bucket with harvest. They run
        # sequentially after it, never concurrently: commit_search then
        # name_bridge. Own-repo mining above uses the core REST bucket, so
        # it does not contend.
        Phase("commit_search", ["foray.enrich.commit_search", "--limit", str(settings.nightly_enrich)], 30, "with_email"),
        # Existing bulk waterfall (gated, above-floor). Resolves LinkedIn
        # URLs onto GitHub rows so resolve.py --tier linkedin_url can join.
        Phase("linkedin", ["foray.enrich.linkedin_bulk", "--limit", str(settings.nightly_enrich)], 20, "with_linkedin"),
        Phase("name_bridge", ["foray.enrich.name_bridge", "--limit", str(settings.nightly_enrich)], 30, "candidates"),
        Phase("verify", ["foray.enrich.verify", "--limit", str(settings.nightly_enrich)], 15, "with_email"),
        # Dry-run only: new auto-apply pairs surface in the night's log.
        # Applying a merge is a human decision (`--tier … --apply`).
        Phase("resolve", ["foray.ingest.resolve", "--scan"], 5, "candidates"),
        # NOT --stale-only: that clause requires an existing score row, so new
        # candidates would never be scored. The default rubric_hash-mismatch
        # predicate covers both new (null hash) and stale rows.
        Phase("score", ["foray.score.run", "--yes",
                        "--max-calls", str(settings.nightly_score_calls),
                        "--concurrency", str(settings.scoring_concurrency)], 90, "scored_pairs"),
        Phase("active_view", ["foray.refresh.active_view"], 10, "candidates"),
    ]


async def _month_spend(conn) -> float:
    pairs = await conn.fetchval(
        "select coalesce(sum(rows_touched), 0) from refresh_runs "
        "where job = 'nightly.score' and started_at > date_trunc('month', now())"
    )
    return float(pairs or 0) * settings.scoring_usd_per_call


async def _run_phase(conn, phase: Phase, remaining_s: float) -> None:
    budget = int(min(phase.budget_min * 60, remaining_s))
    if budget <= 0:
        log.info("nightly.phase_skipped", phase=phase.name, reason="no time left")
        return
    before = await collect(conn)
    async with RefreshRun(conn, f"nightly.{phase.name}") as run:
        # structlog JSON from the child flows straight to the night's log.
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", *phase.argv)
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=budget)
        except TimeoutError:
            proc.terminate()
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=30)
            except TimeoutError:
                proc.kill()
                code = await proc.wait()
            run.notes = f"deadline {budget // 60}m hit"
        after = await collect(conn)
        run.rows = max(0, after[phase.metric] - before[phase.metric])
        run.errors = 1 if code else 0
        log.info("nightly.phase_done", phase=phase.name, code=code, rows=run.rows)


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        got_lock = await conn.fetchval("select pg_try_advisory_lock($1)", LOCK_KEY)
        if not got_lock:
            log.info("nightly.skipped", reason="another nightly run holds the lock")
            return 0
        try:
            phases = _phases()
            if args.dry_run:
                spend = await _month_spend(conn)
                print(f"nightly plan (month-to-date scoring spend ${spend:.2f} / "
                      f"${settings.monthly_spend_cap_usd:.0f} cap):")
                for p in phases:
                    print(f"  {p.name:12s} budget {p.budget_min:3d}m  {' '.join(p.argv)}")
                return 0

            deadline = time.monotonic() + settings.nightly_budget_min * 60
            counts = await collect(conn)
            for phase in phases:
                remaining = deadline - time.monotonic()
                # Backpressure: don't harvest into an already-deep enrich backlog.
                if phase.name == "harvest" and counts["enrich_backlog"] > 2 * settings.nightly_enrich:
                    log.info("nightly.phase_skipped", phase="harvest",
                             reason=f"enrich_backlog {counts['enrich_backlog']} too deep")
                    continue
                # Monthly spend cap: skip scoring if we're over budget.
                if phase.name == "score":
                    spend = await _month_spend(conn)
                    if spend >= settings.monthly_spend_cap_usd:
                        log.info("nightly.phase_skipped", phase="score",
                                 reason=f"month spend ${spend:.2f} >= cap")
                        continue
                try:
                    await _run_phase(conn, phase, remaining)
                except Exception as exc:  # noqa: BLE001 - one phase must not abort the night
                    log.error("nightly.phase_error", phase=phase.name, error=str(exc))
            log.info("nightly.done")
        finally:
            await conn.execute("select pg_advisory_unlock($1)", LOCK_KEY)
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Unattended nightly growth pipeline.")
    ap.add_argument("--dry-run", action="store_true", help="print the phase plan and exit")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
