"""Daily event detection over the watchlist (active subset): catch job changes
via a fresh profile check. Cheap and inside the GraphQL budget for ~5k people.
"""

from __future__ import annotations

import argparse
import asyncio

from ._subset import run_subset


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily job-change detection over the active subset.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run_subset("events", args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
