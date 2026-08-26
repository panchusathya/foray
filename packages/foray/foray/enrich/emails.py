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
  linkedin (65) -- a business address found in a Bright Data export's free
                   text (rare: ~0.2% of records). It sat at 80 -- above commit
                   and above every vendor source -- on the reasoning that a
                   self-published address beats an assertion. What is
                   self-published is the About section; what we store is
                   whatever a regex found in it, which is as likely to be an
                   employer's careers inbox as the person's own. Since
                   UPSERT_EMAIL only replaces `source` on a strict confidence
                   improvement, 80 meant no later real source could ever
                   displace it. See ingest/linkedin_file.py.
  contactout_personal (72) -- a consumer address from ContactOut, which is
                   specialised for them and measured a 70% hit rate against
                   Apollo's 10% on the same ten candidates. Above
                   apollo_personal because of that; below commit and profile,
                   which are self-published and so are evidence rather than a
                   vendor's assertion.
  contactout_work (80) -- below apollo_work (85), same reasoning as linkedin.
  guess (25)    -- reserved; never written by this module and always excluded
                   from the search payload, so a guess can never become a
                   send-to address downstream.

Confidence is "how sure are we this belongs to this person". It is NOT
send-preference: which address outreach uses is decided by the ordering in
foray/api/search.py, which prefers personal regardless of confidence, because a
recruiting pitch must not land in somebody's work inbox.
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
# Public PushEvent payloads no longer include commits[] (GitHub removed the
# embedded commit list, and with it author emails), so mining goes through the
# commits API on the candidate's OWN top repos instead:
#     GET /repos/{login}/{repo}/commits?author={login}
# GitHub resolves ?author= to the account, so every returned commit was
# authored by the candidate — attribution is structural, not heuristic.

GITHUB_REPO_COMMITS = "https://api.github.com/repos/{login}/{repo}/commits"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Local parts that are automation, never a person.
BOT_LOCALPARTS = {"actions", "github-actions", "dependabot", "renovate", "noreply", "bot", "ci"}


def is_usable_commit_email(email: str | None) -> bool:
    """Whether a git-author address is worth storing.

    Noreply and automation addresses are dropped here. ``is_placeholder`` is a
    separate last gate before the write -- a real ``mail@own-domain`` must
    survive this filter.
    """
    value = (email or "").strip().lower()
    if not EMAIL_RE.match(value):
        return False
    if value.endswith("@users.noreply.github.com") or value.endswith("@github.com"):
        return False
    return value.split("@", 1)[0] not in BOT_LOCALPARTS


def extract_own_commit_emails(commits: list[dict]) -> dict[str, int]:
    """{email: occurrences} from an author-filtered commit list of the
    candidate's own repo. Noreply and automation addresses are dropped."""
    found: dict[str, int] = {}
    for item in commits:
        author = (item.get("commit") or {}).get("author") or {}
        email = (author.get("email") or "").strip().lower()
        if not is_usable_commit_email(email):
            continue
        found[email] = found.get(email, 0) + 1
    return found


async def _fetch_own_commits(
    client: httpx.AsyncClient, limiter: RateLimiter, login: str, repo: str
) -> list[dict]:
    await limiter.wait()
    resp = await request_with_retry(
        client,
        "GET",
        GITHUB_REPO_COMMITS.format(login=login, repo=repo),
        log=log,
        params={"author": login, "per_page": 30},
    )
    remaining = resp.headers.get("x-ratelimit-remaining")
    if remaining is not None and int(remaining) < 100:
        import time as _time

        reset = int(resp.headers.get("x-ratelimit-reset", "0"))
        delay = max(1.0, reset - _time.time() + 5)
        log.info("commits.rate_low", remaining=remaining, sleep=round(delay, 1))
        await asyncio.sleep(min(delay, 3600.0))
    body = resp.json()
    return body if isinstance(body, list) else []


async def mine_commit_emails(
    conn,
    *,
    limit: int = 0,
    repos_per_candidate: int = 2,
    concurrency: int = 4,
    dry_run: bool = False,
) -> int:
    """Mine commit-author emails from the candidate's own top repos, author-
    filtered by GitHub. Core REST quota (5,000/hr) — a different bucket from
    the Search API's 30/min."""
    settings.require("github_token")
    rows = await conn.fetch(
        f"""
        select c.id, c.login, s.top_repos
        from candidates c
        join candidate_signals s on s.candidate_id = c.id
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

    import json as _json

    limiter = RateLimiter(settings.github_rest_rpm)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    stored = 0
    checked = 0
    unqueryable = 0

    async def fetch_one(client: httpx.AsyncClient, row) -> tuple:
        async with semaphore:
            top = row["top_repos"]
            top = _json.loads(top) if isinstance(top, str) else (top or [])
            repos = [r.get("name") for r in top if r.get("name")][:repos_per_candidate]
            if not repos:
                # No repos to mine, so no call was made and there is nothing to
                # cache. `found = {}` here would be indistinguishable from "we
                # asked and there was nothing", which stamps the 180-day cursor
                # and hides the candidate for six months -- including from the
                # run right after enrich.profiles finally lands their repos.
                # Same class as the GitHub-429 stamp below; None says so.
                return row, None, None
            found: dict[str, int] = {}
            try:
                for repo in repos:
                    commits = await _fetch_own_commits(client, limiter, row["login"], repo)
                    for email, count in extract_own_commit_emails(commits).items():
                        found[email] = found.get(email, 0) + count
                    if found:
                        break  # first repo with usable addresses is enough
                return row, found, None
            except Exception as exc:  # noqa: BLE001 - recorded per-row, never fatal
                return row, {}, exc

    async def _persist(row, found: dict | None, exc) -> None:
        nonlocal checked, stored, unqueryable
        if found is None and exc is None:
            unqueryable += 1
            return
        checked += 1
        if exc is not None:
            await conn.execute(
                "insert into enrich_errors (candidate_id, login, stage, error) "
                "values ($1, $2, 'commit_email', $3)",
                row["id"],
                row["login"],
                str(exc)[:500],
            )
            # Do not stamp the negative cache on a vendor error: a 429 or
            # timeout is not "we checked, nothing there", and the 180-day
            # cursor would skip this person until it expired.
            return
        for email, occurrences in (found or {}).items():
            await conn.execute(
                UPSERT_EMAIL,
                row["id"],
                email,
                "commit",
                is_work_domain(email),
                75,
                False,
                f"authored commits in own repo ({occurrences} commit(s))",
                occurrences,
            )
            stored += 1
        # Stamp hit or miss: the negative cache and the resume cursor.
        await conn.execute(
            "update candidates set commit_emails_checked_at = now() where id = $1", row["id"]
        )

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "foray-enricher",
        },
    ) as client:
        with Heartbeat(
            log, lambda: {"checked": checked, "stored": stored, "no_repos": unqueryable}
        ):
            # Network fans out (semaphore-bounded); writes happen on this one
            # connection as each fetch completes, so a deadline kill keeps all
            # progress up to that point instead of losing the whole batch.
            tasks = [asyncio.ensure_future(fetch_one(client, r)) for r in rows]
            for coro in asyncio.as_completed(tasks):
                row, found, exc = await coro
                await _persist(row, found, exc)

    log.info("commit_mining.done", checked=checked, stored=stored, no_repos=unqueryable)
    print(
        f"commit mining: checked {checked} candidates, stored {stored} addresses, "
        f"{unqueryable} skipped with no repos yet"
    )
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
                    repos_per_candidate=args.repos,
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
    ap.add_argument("--repos", type=int, default=2, help="own repos to try per candidate")
    ap.add_argument("--concurrency", type=int, default=4, help="parallel commit fetches")
    ap.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
