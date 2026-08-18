"""Weekly refresh of volatile fields (employer, title, location, last_push) over
the active subset only -- never the whole pool.
"""

from __future__ import annotations

import argparse
import asyncio

from ._subset import run_subset


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly volatile-field refresh over the active subset.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run_subset("volatile", args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
