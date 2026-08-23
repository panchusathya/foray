"""One source of truth for pipeline state, used by both the CLI and /stats.

    python -m foray.status [--json] [--runs 8]
"""

from __future__ import annotations

import argparse
import asyncio

from .db import close_pool, get_pool
from .search_doc import HAS_LEXICAL_DOC_SQL

# f-string only for HAS_LEXICAL_DOC_SQL — "has a lexical document" is defined
# once in search_doc so status, the count query, and the health probes cannot
# drift (a bare `search_tsv is not null` counted indexed-but-EMPTY tsvectors).
# The constant assumes the `s` alias, hence `candidate_signals s` below.
_COUNTS_SQL = f"""
select
  (select count(*) from candidates where merged_into is null)             as candidates,
  (select count(*) from candidates where source in ('linkedin','both')
        and merged_into is null)                                           as linkedin_pool,
  (select count(*) from raw_profiles where source = 'github')             as enriched,
  (select count(*) from candidate_signals)                                 as derived,
  (select count(*) from candidates where embedding is not null)            as embedded,
  (select count(*) from candidate_signals s where {HAS_LEXICAL_DOC_SQL})   as lexical_indexed,
  (select count(distinct candidate_id) from archetype_scores)              as scored_candidates,
  (select count(*) from archetype_scores)                                  as scored_pairs,
  (select count(distinct candidate_id) from candidate_emails
        where source <> 'guess')                                           as with_email,
  (select count(*) from candidates where linkedin_url is not null)         as with_linkedin,
  (select count(*) from candidates c
     left join raw_profiles r on r.candidate_id = c.id and r.source = 'github'
     where c.source in ('github', 'both')
       and c.merged_into is null
       and r.candidate_id is null
       and not exists (select 1 from enrich_errors e
                       where e.candidate_id = c.id and e.stage = 'fetch'
                         and e.created_at > now() - interval '14 days'))    as enrich_backlog,
  (select count(*) from harvest_shards where status = 'done')              as shards_done,
  (select count(*) from harvest_shards where status = 'pending')           as shards_pending,
  (select count(*) from harvest_shards where status = 'truncated')         as shards_truncated,
  (select count(*) from enrich_errors  where created_at > now() - interval '7 days') as enrich_errors_7d,
  (select count(*) from scoring_errors where created_at > now() - interval '7 days') as scoring_errors_7d
"""


async def collect(conn) -> dict:
    row = await conn.fetchrow(_COUNTS_SQL)
    return dict(row)


async def recent_runs(conn, n: int) -> list[dict]:
    rows = await conn.fetch(
        """
        select job, started_at, ended_at, rows_touched, api_calls, errors, notes,
               (ended_at is null and started_at < now() - interval '12 hours') as crashed
        from refresh_runs order by id desc limit $1
        """,
        n,
    )
    return [dict(r) for r in rows]


def _nights_to_100k(counts: dict, runs: list[dict]) -> str:
    remaining = 100_000 - counts["candidates"]
    if remaining <= 0:
        return "target reached"
    # Last night's net candidate growth = rows_touched of the most recent
    # nightly.harvest run (a rough proxy; harvest rows == candidates added).
    per_night = next(
        (r["rows_touched"] for r in runs if r["job"] == "nightly.harvest" and r["rows_touched"]),
        None,
    )
    if not per_night:
        return f"{remaining:,} to go (no nightly.harvest run yet to estimate rate)"
    return f"{remaining:,} to go, ~{-(-remaining // per_night)} nights at {per_night:,}/night"


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            counts = await collect(conn)
            runs = await recent_runs(conn, args.runs)
    finally:
        await close_pool()

    if args.json:
        import json

        print(json.dumps({"counts": counts, "runs": runs}, default=str, indent=2))
        return 0

    print("Foray pipeline status")
    print("---------------------")
    for key in (
        "candidates", "linkedin_pool", "enriched", "derived", "embedded",
        "lexical_indexed", "scored_candidates", "scored_pairs", "with_email", "with_linkedin",
        "enrich_backlog", "shards_done", "shards_pending", "shards_truncated",
        "enrich_errors_7d", "scoring_errors_7d",
    ):
        print(f"  {key:20s} {counts[key]:>10,}")
    print(f"  {'to 100k':20s} {_nights_to_100k(counts, runs)}")
    print("\nrecent refresh_runs")
    for r in runs:
        dur = (r["ended_at"] - r["started_at"]) if r["ended_at"] else None
        state = "CRASHED" if r["crashed"] else (f"{dur}" if dur else "running")
        print(
            f"  {r['job']:22s} {state:>16}  rows={r['rows_touched'] or 0:>6}  "
            f"api={r['api_calls'] or 0:>5}  err={r['errors'] or 0}"
            + (f"  {r['notes']}" if r["notes"] else "")
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Foray pipeline status.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--runs", type=int, default=8, help="recent refresh_runs to show")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
