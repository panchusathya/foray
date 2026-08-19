"""Contact email acquisition.

Sources, in confidence order (see UPSERT: an address is never downgraded):

  profile (90)  -- the address the person chose to publish on their GitHub
                   profile. Already harvested into raw_profiles.data; the
                   backfill promotes it into candidate_emails with zero API
                   calls.
  commit  (75/55) -- mined from public push events (PR 2; not yet implemented
                   here). 75 needs author-name/login attribution, 55 needs the
                   same address across >=2 pushes.
  apollo_work / apollo_personal (85/70) -- captured by enrich.apollo (PR 2).
  guess (25)    -- reserved; never written by this module and always excluded
                   from the search payload, so a guess can never become a
                   send-to address downstream.
"""

from __future__ import annotations

import argparse
import asyncio

from ..config import settings  # noqa: F401 - parity with sibling modules; used in PR 2
from ..db import close_pool, get_pool
from ..log import get_logger
from .employer import freemail_domains

log = get_logger("enrich.emails")

# Matches normalize-side validation in the main app (juicebox/enrich/normalize.py).
EMAIL_SQL_RE = r"^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$"

UPSERT_EMAIL = """
insert into candidate_emails
    (candidate_id, email, source, is_work, confidence, verified, evidence, occurrences)
values ($1, $2, $3, $4, $5, $6, $7, $8)
on conflict (candidate_id, email) do update set
    occurrences  = candidate_emails.occurrences + excluded.occurrences,
    last_seen_at = now(),
    verified     = candidate_emails.verified or excluded.verified,
    source       = case when excluded.confidence > candidate_emails.confidence
                        then excluded.source else candidate_emails.source end,
    confidence   = greatest(candidate_emails.confidence, excluded.confidence),
    evidence     = coalesce(candidate_emails.evidence, excluded.evidence)
"""

_PROFILE_SELECT = f"""
select r.candidate_id,
       lower(trim(r.data->>'email')) as email
from raw_profiles r
where coalesce(trim(r.data->>'email'), '') <> ''
  and lower(trim(r.data->>'email')) ~ '{EMAIL_SQL_RE}'
  and lower(trim(r.data->>'email')) !~ '@users\\.noreply\\.github\\.com$'
"""


async def backfill_profile_emails(conn, *, limit: int = 0, dry_run: bool = False) -> int:
    """Promote raw_profiles.data->>'email' into candidate_emails. No API calls."""
    if dry_run:
        count = await conn.fetchval(f"select count(*) from ({_PROFILE_SELECT}) q")
        print(f"profile backfill would consider {count} addresses (dry run)")
        return 0

    freemail = sorted(freemail_domains())
    inserted = await conn.fetchval(
        f"""
        with src as ({_PROFILE_SELECT} {"limit $2" if limit else ""}),
        ins as (
            insert into candidate_emails
                (candidate_id, email, source, is_work, confidence, evidence)
            select candidate_id,
                   email,
                   'profile',
                   split_part(email, '@', 2) <> all($1::text[]),
                   90,
                   'github profile email field'
            from src
            on conflict (candidate_id, email) do nothing
            returning 1
        )
        select count(*) from ins
        """,
        freemail,
        *([limit] if limit else []),
    )

    # Same pass: a non-freemail profile-email domain is the employer's domain.
    domains_set = await conn.fetchval(
        f"""
        with src as ({_PROFILE_SELECT}),
        upd as (
            update candidate_signals s
            set employer_domain = split_part(src.email, '@', 2)
            from src
            where s.candidate_id = src.candidate_id
              and s.employer_domain is null
              and split_part(src.email, '@', 2) <> all($1::text[])
            returning 1
        )
        select count(*) from upd
        """,
        freemail,
    )

    log.info("profile_backfill.done", inserted=inserted, employer_domains=domains_set)
    print(f"profile backfill: {inserted} new addresses, {domains_set} employer domains set")
    return int(inserted)


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            if args.source in ("profile", "all"):
                await backfill_profile_emails(conn, limit=args.limit, dry_run=args.dry_run)
            if args.source in ("commit", "all") and args.source != "profile":
                raise SystemExit(
                    "commit-email mining is not implemented yet (lands in the next change); "
                    "run with --source profile"
                )
    finally:
        await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Acquire contact emails for candidates.")
    ap.add_argument("--source", choices=["profile", "commit", "all"], default="profile")
    ap.add_argument("--limit", type=int, default=0, help="max addresses (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
