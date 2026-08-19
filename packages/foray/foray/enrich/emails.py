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
import re

import httpx

from ..config import settings
from ..db import close_pool, get_pool
from ..log import Heartbeat, get_logger
from ..ratelimit import RateLimiter
from ._http import request_with_retry
from .employer import freemail_domains, is_work_domain

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


# ------------------------------------------------------------- commit mining --

GITHUB_EVENTS = "https://api.github.com/users/{login}/events/public"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NOT_ALNUM = re.compile(r"[^a-z0-9]")

# Local parts that are automation, never a person.
BOT_LOCALPARTS = {"actions", "github-actions", "dependabot", "renovate", "noreply", "bot", "ci"}


def _norm(text: str | None) -> str:
    return _NOT_ALNUM.sub("", (text or "").lower())


def extract_commit_emails(
    events: list[dict], *, candidate_name: str | None, login: str
) -> dict[str, dict]:
    """{email: {pushes, name_match}} from public events, with the attribution guard.

    PushEvent.commits[].author is whoever AUTHORED the commit — a merge or a
    rebase carries a colleague's address. Callers only accept an address when
    the author name matches the candidate (or the local part matches the
    login), or the same address appears across >=2 distinct pushes.
    """
    norm_name = _norm(candidate_name)
    norm_login = _norm(login)
    found: dict[str, dict] = {}
    for event in events:
        if event.get("type") != "PushEvent":
            continue
        push_id = event.get("id")
        for commit in (event.get("payload") or {}).get("commits") or []:
            author = commit.get("author") or {}
            email = (author.get("email") or "").strip().lower()
            author_name = author.get("name") or ""
            if not EMAIL_RE.match(email):
                continue
            if email.endswith("@users.noreply.github.com"):
                continue
            local = email.split("@", 1)[0]
            if local in BOT_LOCALPARTS or author_name.endswith("[bot]"):
                continue
            entry = found.setdefault(email, {"pushes": set(), "name_match": False})
            entry["pushes"].add(push_id)
            norm_author = _norm(author_name)
            if (norm_name and norm_author == norm_name) or _norm(local) == norm_login:
                entry["name_match"] = True
    return found


def accepted_commit_emails(found: dict[str, dict]) -> list[tuple[str, int, str]]:
    """[(email, confidence, evidence)] applying the attribution thresholds."""
    accepted = []
    for email, entry in found.items():
        pushes = len(entry["pushes"])
        if entry["name_match"]:
            accepted.append((email, 75, f"commit author, name match, {pushes} push(es)"))
        elif pushes >= 2:
            accepted.append((email, 55, f"commit author across {pushes} pushes, no name match"))
    return accepted


async def _fetch_events(client: httpx.AsyncClient, limiter: RateLimiter, login: str, pages: int) -> list[dict]:
    events: list[dict] = []
    for page in range(1, pages + 1):
        await limiter.wait()
        resp = await request_with_retry(
            client,
            "GET",
            GITHUB_EVENTS.format(login=login),
            log=log,
            params={"per_page": 100, "page": page},
        )
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is not None and int(remaining) < 100:
            import time as _time

            reset = int(resp.headers.get("x-ratelimit-reset", "0"))
            delay = max(1.0, reset - _time.time() + 5)
            log.info("events.rate_low", remaining=remaining, sleep=round(delay, 1))
            await asyncio.sleep(min(delay, 3600.0))
        batch = resp.json()
        if not batch:
            break
        events.extend(batch)
        if len(batch) < 100:
            break
    return events


async def mine_commit_emails(
    conn,
    *,
    limit: int = 0,
    pages: int = 1,
    concurrency: int = 4,
    dry_run: bool = False,
) -> int:
    """Mine commit-author emails from public push events. Core REST quota
    (5,000/hr) — a different bucket from the Search API's 30/min."""
    settings.require("github_token")
    rows = await conn.fetch(
        f"""
        select c.id, c.login, c.name
        from candidates c
        where c.commit_emails_checked_at is null
           or c.commit_emails_checked_at < now() - interval '180 days'
        order by c.id
        {"limit $1" if limit else ""}
        """,
        *([limit] if limit else []),
    )
    if dry_run:
        print(f"commit mining would check {len(rows)} candidates (dry run)")
        return 0

    limiter = RateLimiter(settings.github_rest_rpm)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    stored = 0
    checked = 0

    async def fetch_one(client: httpx.AsyncClient, row) -> tuple:
        async with semaphore:
            try:
                events = await _fetch_events(client, limiter, row["login"], pages)
                return row, extract_commit_emails(
                    events, candidate_name=row["name"], login=row["login"]
                ), None
            except Exception as exc:  # noqa: BLE001 - recorded per-row, never fatal
                return row, {}, exc

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "foray-enricher",
        },
    ) as client:
        with Heartbeat(log, lambda: {"checked": checked, "stored": stored}):
            # Network fans out; DB writes stay serial on this one connection
            # (asyncpg connections reject concurrent queries).
            results = await asyncio.gather(*(fetch_one(client, r) for r in rows))

    for row, found, exc in results:
        checked += 1
        if exc is not None:
            await conn.execute(
                "insert into enrich_errors (candidate_id, login, stage, error) "
                "values ($1, $2, 'commit_email', $3)",
                row["id"],
                row["login"],
                str(exc)[:500],
            )
        else:
            for email, confidence, evidence in accepted_commit_emails(found):
                await conn.execute(
                    UPSERT_EMAIL,
                    row["id"],
                    email,
                    "commit",
                    is_work_domain(email),
                    confidence,
                    False,
                    evidence,
                    len(found[email]["pushes"]),
                )
                stored += 1
        # Stamp hit or miss: the negative cache and the resume cursor.
        await conn.execute(
            "update candidates set commit_emails_checked_at = now() where id = $1", row["id"]
        )

    log.info("commit_mining.done", checked=checked, stored=stored)
    print(f"commit mining: checked {checked} candidates, stored {stored} addresses")
    return stored


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            if args.source in ("profile", "all"):
                await backfill_profile_emails(conn, limit=args.limit, dry_run=args.dry_run)
            if args.source in ("commit", "all"):
                await mine_commit_emails(
                    conn,
                    limit=args.limit,
                    pages=args.pages,
                    concurrency=args.concurrency,
                    dry_run=args.dry_run,
                )
    finally:
        await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Acquire contact emails for candidates.")
    ap.add_argument("--source", choices=["profile", "commit", "all"], default="profile")
    ap.add_argument("--limit", type=int, default=0, help="max addresses/candidates (0 = all)")
    ap.add_argument("--pages", type=int, default=1, help="event pages per candidate (max 3)")
    ap.add_argument("--concurrency", type=int, default=4, help="parallel event fetches")
    ap.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
