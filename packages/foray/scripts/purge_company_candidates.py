"""Purge LinkedIn company pages mis-ingested as candidates.

The Bright Data export mixes company pages into the person stream (a real
search surfaced candidate "Infrastructure United Inc. (IU)"). Ingest now drops
them at the mapper (linkedin_map.looks_like_company); this script backfills
that fix over rows already in the database.

Dry-run by default: prints the matches and changes nothing. ``--delete --yes``
actually removes them.

Cascade audit (packages/foray/migrations): candidate_emails (003),
raw_profiles (001), candidate_signals (001), archetype_scores (001),
shortlist_events (001) and merge_events (005) all declare ON DELETE CASCADE on
their candidate FK, so a single DELETE FROM candidates removes every child.
Two non-cascading edges were checked and handled:
  * candidates.merged_into (005) is a self-FK with NO delete action --
    deleting a merge TARGET would raise, so such ids are excluded and reported
    instead (a company page should never be one; if it is, that merge needs a
    human).
  * enrich_errors / scoring_errors carry a bare candidate_id with no FK at
    all; their orphaned diagnostic rows are harmless and left alone.
"""

from __future__ import annotations

import argparse
import asyncio

from foray.db import close_pool, get_pool
from foray.ingest.linkedin_map import looks_like_company

SAMPLE_SIZE = 20  # enough of a print sample to eyeball the heuristic's aim


async def run(args: argparse.Namespace) -> int:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        select id, login, name
          from candidates
         where source = 'linkedin'
           and merged_into is null
         order by id
        """
    )
    # The heuristic runs in Python, deliberately NOT re-expressed in SQL:
    # linkedin_map.looks_like_company is the single implementation serving
    # both ingest and this cleanup, so the two can never drift apart.
    matched = [row for row in rows if looks_like_company(row["name"])]
    print(f"scanned {len(rows)} linkedin candidates; {len(matched)} look like company pages")
    for row in matched[:SAMPLE_SIZE]:
        print(f"  id={row['id']}  {row['login']}  {row['name']!r}")
    if len(matched) > SAMPLE_SIZE:
        print(f"  ... and {len(matched) - SAMPLE_SIZE} more")

    if not args.delete:
        print("dry run: nothing changed (pass --delete --yes to remove them)")
        return 0
    if not matched:
        print("nothing to delete")
        return 0

    ids = [row["id"] for row in matched]
    async with pool.acquire() as conn, conn.transaction():
        # merged_into is a self-FK with no ON DELETE action: deleting a
        # candidate that some other row merged into would raise. A company
        # page should never be a merge target; if one somehow is, skip it
        # loudly rather than fail the whole transaction.
        blocked = {
            row["merged_into"]
            for row in await conn.fetch(
                "select distinct merged_into from candidates"
                " where merged_into = any($1::bigint[])",
                ids,
            )
        }
        if blocked:
            print(
                f"skipping {len(blocked)} merge-target ids "
                f"(other candidates point merged_into at them): {sorted(blocked)}"
            )
            ids = [i for i in ids if i not in blocked]
        deleted = await conn.fetch(
            "delete from candidates where id = any($1::bigint[])"
            " returning id, login, name",
            ids,
        )
    print(f"deleted {len(deleted)} candidates (children removed via ON DELETE CASCADE)")
    for row in deleted[:SAMPLE_SIZE]:
        print(f"  id={row['id']}  {row['login']}  {row['name']!r}")
    if len(deleted) > SAMPLE_SIZE:
        print(f"  ... and {len(deleted) - SAMPLE_SIZE} more")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--delete", action="store_true", help="delete the matches (default: dry run)"
    )
    ap.add_argument(
        "--yes", action="store_true", help="required with --delete; confirms the deletion"
    )
    args = ap.parse_args()
    # Destructive and unscoped over the whole linkedin corpus -- a fat-fingered
    # --delete alone must not be enough, hence the explicit --yes handshake.
    if args.delete and not args.yes:
        ap.error("--delete is destructive; add --yes to confirm")

    async def _run() -> int:
        try:
            return await run(args)
        finally:
            await close_pool()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
