"""Refresh the volatile fields of the active subset via one freshness call each."""

from __future__ import annotations

from typing import Awaitable, Callable

from ..config import settings
from ..db import close_pool, get_pool
from ..enrich.freshness import freshness_check
from ..log import get_logger
from .common import RefreshRun, active_logins

log = get_logger("refresh.subset")


async def run_subset(
    job: str,
    dry_run: bool,
    *,
    fetch_node: Callable[[str], Awaitable[dict | None]] | None = None,
) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with RefreshRun(conn, job) as run:
            logins = await active_logins(conn)
            if dry_run:
                print(f"[dry-run] {job}: would refresh {len(logins)} active candidates")
                run.notes = f"dry-run: {len(logins)} active"
                return 0
            if fetch_node is None and not settings.github_token:
                print(f"{job}: GITHUB_TOKEN not set; skipping ({len(logins)} active candidates)")
                run.notes = "skipped: no GITHUB_TOKEN"
                return 0
            for login in logins:
                try:
                    result = await freshness_check(conn, login, fetch_node=fetch_node)
                    run.api_calls += 1
                    if result.get("changed"):
                        run.rows += 1
                except Exception as exc:  # noqa: BLE001 - isolate one candidate
                    run.errors += 1
                    log.error("refresh.error", job=job, login=login, error=str(exc))
            print(f"{job}: {run.rows} employer changes across {run.api_calls} checks")
            changed = run.rows
    await close_pool()
    return changed
