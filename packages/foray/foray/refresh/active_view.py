"""Nightly recompute of the active_candidates materialized view."""

from __future__ import annotations

import argparse
import asyncio

import asyncpg

from ..db import close_pool, get_pool
from .common import RefreshRun


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn, RefreshRun(conn, "active_view") as run:
        current = await conn.fetchval("select count(*) from active_candidates")
        if args.dry_run:
            print(f"[dry-run] would refresh active_candidates (currently {current} rows)")
            run.notes = f"dry-run: {current} rows"
            return 0
        try:
            await conn.execute("refresh materialized view concurrently active_candidates")
        except asyncpg.ObjectNotInPrerequisiteStateError:
            # CONCURRENTLY needs a prior populated state; fall back on first run.
            await conn.execute("refresh materialized view active_candidates")
        run.rows = await conn.fetchval("select count(*) from active_candidates")
        print(f"active_candidates refreshed: {run.rows} rows")
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Nightly refresh of active_candidates.")
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
