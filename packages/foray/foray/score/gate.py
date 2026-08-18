"""Report the gated pool size for an archetype -- the cost control. If it
exceeds ~10,000, tighten the gate before spending money.
"""

from __future__ import annotations

import argparse
import asyncio

from ..db import close_pool, get_pool
from .archetypes import load_archetypes, sync_archetypes
from .common import PER_CALL_USD, gate_count


async def amain(args: argparse.Namespace) -> int:
    archetypes = load_archetypes()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await sync_archetypes(conn, archetypes)  # also validates gates
        targets = args.archetype.split(",") if args.archetype else list(archetypes)
        print(f"{'archetype':22s} {'gated':>8s} {'est $':>10s}")
        total = 0
        for aid in targets:
            arch = archetypes.get(aid)
            if arch is None:
                print(f"{aid:22s}  unknown archetype")
                continue
            n = await gate_count(conn, arch)
            total += n
            warn = "  <-- >10k, tighten gate" if n > 10000 else ""
            print(f"{aid:22s} {n:8d} {n * PER_CALL_USD:10.2f}{warn}")
        if not args.archetype:
            print(f"{'TOTAL':22s} {total:8d} {total * PER_CALL_USD:10.2f}")
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Count the gated pool per archetype.")
    ap.add_argument("--archetype", help="comma-separated ids (default: all)")
    ap.add_argument("--count", action="store_true", help="(accepted for parity; always counts)")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
