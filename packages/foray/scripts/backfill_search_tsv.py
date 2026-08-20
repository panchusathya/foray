"""Backfill candidate_signals.search_tsv without touching derived_at.

A full ``python -m foray.enrich.derive`` would bump derived_at on every row and
the next nightly embed would pull the whole corpus into memory (embed/run.py
records OOMing a 4 GB VM at 40-50k). This script UPDATEs only search_tsv.

Two passes:
  default     classes A/B/C + c.name (already-populated columns). Fast.
  --with-prose  also GitHub bio + LinkedIn about/position (TOAST-heavy).

Interruptible and resumable: a commit per batch; rows with a non-null
search_tsv are skipped. After a full run, operators should (outside this
script, outside a transaction):

    reindex index concurrently ix_signals_search_tsv;
    vacuum analyze candidate_signals;

The incremental UPDATEs grow the GIN through its pending list; REINDEX
rebuilds it cleanly and ANALYZE gives the planner statistics on the new
column.

Run outside the 23:00 UTC nightly window. Hold the same flock as
scripts/import_linkedin.sh if both might run on the box.
"""

from __future__ import annotations

import argparse
import sys
import time

from foray.db import sync_connect
from foray.search_doc import SEARCH_TSV_SQL, SEARCH_TSV_SQL_FAST

FAST_SQL = f"""
update candidate_signals s
   set search_tsv = {SEARCH_TSV_SQL_FAST}
  from candidates c
 where s.candidate_id = c.id
   and s.search_tsv is null
   and s.candidate_id = any(%s)
"""

PROSE_SQL = f"""
update candidate_signals s
   set search_tsv = {SEARCH_TSV_SQL}
  from candidates c
  left join raw_profiles gh on gh.candidate_id = c.id and gh.source = 'github'
  left join raw_profiles li on li.candidate_id = c.id and li.source = 'linkedin'
 where s.candidate_id = c.id
   and s.candidate_id = any(%s)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--with-prose", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    sql = PROSE_SQL if args.with_prose else FAST_SQL
    conn = sync_connect()
    processed = 0
    last_id = 0
    started = time.monotonic()
    try:
        while True:
            with conn.cursor() as cur:
                if args.with_prose:
                    cur.execute(
                        """
                        select s.candidate_id
                          from candidate_signals s
                         where s.candidate_id > %s
                         order by s.candidate_id
                         limit %s
                        """,
                        (last_id, args.batch_size),
                    )
                else:
                    cur.execute(
                        """
                        select s.candidate_id
                          from candidate_signals s
                         where s.search_tsv is null
                           and s.candidate_id > %s
                         order by s.candidate_id
                         limit %s
                        """,
                        (last_id, args.batch_size),
                    )
                ids = [row[0] for row in cur.fetchall()]
            if not ids:
                break
            with conn.cursor() as cur:
                cur.execute(sql, (ids,))
            conn.commit()
            processed += len(ids)
            last_id = ids[-1]
            elapsed = time.monotonic() - started
            print(f"backfill_search_tsv: {processed} rows (id<={last_id}) in {elapsed:.1f}s", flush=True)
            if args.limit and processed >= args.limit:
                break
            if len(ids) < args.batch_size:
                break
    except KeyboardInterrupt:
        conn.rollback()
        print(f"interrupted after {processed} rows; rerun to resume", file=sys.stderr)
        return 130
    finally:
        conn.close()
    print(f"done: {processed} rows")
    print("next: reindex index concurrently ix_signals_search_tsv;")
    print("      vacuum analyze candidate_signals;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
