"""Bridge LinkedIn-only engineers to GitHub identities via commit-name search.

A LinkedIn row has a display name, not a GitHub login. Commit search
``author-name:"Full Name"`` returns git-author emails plus GitHub's
``author.login`` attribution. A bare name match is not identity -- "David Lee"
will match many people -- so a login is accepted only when corroborated:

  * its GitHub profile (bio/blog) contains the LinkedIn slug, or
  * its company matches ``candidate_signals.employer``, or
  * its location matches the LinkedIn metro / location_raw, or
  * the name is rare in the result set (a single distinct login across
    ``FORAY_NAME_BRIDGE_MIN_COMMITS`` commits).

On a corroborated hit this module:

  1. upserts a GitHub ``candidates`` row if that login is not already in the
     pool (``GET /users/{login}``, core REST -- harvest is not a prerequisite);
  2. writes GitHub-attributed commit emails onto **that GitHub row**;
  3. queues a ``candidate_merges`` row at tier ``commit_name``, review-only.

It never writes an address onto the LinkedIn row and never auto-applies the
merge. A wrong address here would become ``primary_email`` and then a send
to a stranger. Once a human applies the merge, the GitHub row's addresses
flow through ``apply_merge`` for free.

Search sits on ``FORAY_GITHUB_RPM``; the user fetch uses the core REST
bucket. Nightly runs this after harvest and commit_search so the three
search-bucket jobs never overlap.
"""

from __future__ import annotations

import argparse
import asyncio
import re

import httpx

from ..config import settings
from ..db import close_pool, get_pool
from ..ingest.resolve import queue_pair
from ..log import Heartbeat, get_logger
from ..ratelimit import RateLimiter
from ._http import request_with_retry
from .commit_search import (
    _SEARCH_HEADERS,
    GITHUB_COMMIT_SEARCH,
    extract_attributed_commit_emails,
)
from .contacts import is_placeholder
from .emails import UPSERT_EMAIL
from .employer import is_work_domain
from .linkedin import find_linkedin

log = get_logger("enrich.name_bridge")

GITHUB_USER = "https://api.github.com/users/{login}"

# Titles that imply a public git corpus is even possible. Matches the
# engineer-titled slice of the LinkedIn pool (~14k of ~197k).
ENGINEER_TITLE_SQL = r"(engineer|developer|\mswe\M|\msde\M|\msre\M|programmer|devops|mlops|software)"

_COMPANY_SUFFIX = re.compile(
    r"[,.]?\s*\b(incorporated|corporation|company|inc|llc|ltd|corp|co)\.?\s*$",
    re.IGNORECASE,
)


def name_search_query(name: str | None) -> str | None:
    """``author-name:"Full Name"``, or None if the name cannot identify anyone."""
    cleaned = " ".join((name or "").split())
    if len(cleaned) < 5 or " " not in cleaned:
        return None
    escaped = cleaned.replace('"', "")
    if len(escaped) < 5 or " " not in escaped:
        return None
    return f'author-name:"{escaped}"'


def _norm_company(value: str | None) -> str:
    text = (value or "").strip().lstrip("@").strip()
    text = _COMPANY_SUFFIX.sub("", text).strip()
    return text.lower()


def companies_match(github_company: str | None, employer: str | None) -> bool:
    left, right = _norm_company(github_company), _norm_company(employer)
    if not left or not right or min(len(left), len(right)) < 3:
        return False
    return left == right or left in right or right in left


def locations_match(
    github_location: str | None, metro: str | None, location_raw: str | None
) -> bool:
    gh = (github_location or "").strip().lower()
    if len(gh) < 4:
        return False
    for token in (metro, location_raw):
        t = (token or "").strip().lower()
        if len(t) >= 4 and (t in gh or gh in t):
            return True
    return False


def profile_has_slug(user: dict, slug: str | None) -> bool:
    """True when the GitHub profile points at this LinkedIn slug."""
    if not slug:
        return False
    slug = slug.strip().lower()
    blob = " ".join(
        part for part in (user.get("bio"), user.get("blog"), user.get("html_url")) if part
    )
    if slug and slug in blob.lower():
        return True
    url = find_linkedin(blob)
    return bool(url and slug in url.lower())


def collapse_hits_by_login(items: list[dict]) -> dict[str, dict]:
    """{login: {commits, emails, github_id}} from a name-search page.

    Unattributed commits (null ``author``) are dropped -- without a login there
    is nobody to mint or merge.
    """
    by_login: dict[str, dict] = {}
    for item in items:
        author = item.get("author") or {}
        login = (author.get("login") or "").strip()
        if not login:
            continue
        bucket = by_login.setdefault(
            login, {"commits": 0, "emails": {}, "github_id": author.get("id")}
        )
        bucket["commits"] += 1
        for email, agg in extract_attributed_commit_emails([item], login).items():
            cur = bucket["emails"].setdefault(email, {"occurrences": 0, "last_date": ""})
            cur["occurrences"] += agg["occurrences"]
            if agg["last_date"] > (cur["last_date"] or ""):
                cur["last_date"] = agg["last_date"]
    return by_login


def corroborate(
    *,
    login: str,
    bucket: dict,
    user: dict,
    linkedin: dict,
    sole_login: bool,
    min_commits: int,
) -> list[str]:
    """Reasons this GitHub login is the LinkedIn person. Empty = reject."""
    reasons: list[str] = []
    if profile_has_slug(user, linkedin.get("linkedin_slug")):
        reasons.append("linkedin_slug")
    if companies_match(user.get("company"), linkedin.get("employer")):
        reasons.append("employer")
    if locations_match(
        user.get("location"), linkedin.get("location_metro"), linkedin.get("location_raw")
    ):
        reasons.append("location")
    if sole_login and int(bucket.get("commits") or 0) >= min_commits:
        reasons.append("rare_name")
    return reasons


async def _search_name(
    client: httpx.AsyncClient, limiter: RateLimiter, query: str
) -> list[dict]:
    await limiter.wait()
    resp = await request_with_retry(
        client,
        "GET",
        GITHUB_COMMIT_SEARCH,
        log=log,
        params={"q": query, "per_page": 100, "sort": "author-date", "order": "desc"},
    )
    body = resp.json() if resp.content else {}
    items = body.get("items") if isinstance(body, dict) else None
    return items if isinstance(items, list) else []


async def fetch_github_user(
    client: httpx.AsyncClient, limiter: RateLimiter, login: str
) -> dict | None:
    """Core REST. 404 is a miss, not an error — ``request_with_retry`` would
    raise it, so this path handles the status itself."""
    await limiter.wait()
    resp = await client.get(
        GITHUB_USER.format(login=login),
        headers={"Accept": "application/vnd.github+json"},
    )
    if resp.status_code == 404:
        return None
    if resp.status_code in {403, 429, 500, 502, 503, 504}:
        # Ride the shared retry policy for transients.
        resp = await request_with_retry(
            client,
            "GET",
            GITHUB_USER.format(login=login),
            log=log,
            headers={"Accept": "application/vnd.github+json"},
        )
    resp.raise_for_status()
    body = resp.json() if resp.content else {}
    return body if isinstance(body, dict) else None


async def ensure_github_candidate(conn, user: dict) -> tuple[int, bool]:
    """Insert a GitHub identity so ``candidate_merges`` has an FK to point at.

    Harvest is not a prerequisite: a name-bridge hit on a login we have never
    sharded still has to become a row, or the merge has nowhere to land and
    there is nowhere legal to put the address. Returns ``(id, created)``.
    """
    login = user["login"]
    existing = await conn.fetchval(
        "select id from candidates where lower(login) = lower($1)", login
    )
    if existing:
        return int(existing), False
    row = await conn.fetchrow(
        """
        insert into candidates
            (login, github_id, name, location_raw, source, discovery_shard)
        values ($1, $2::bigint, $3::text, $4::text, 'github', 'name_bridge')
        on conflict (login) do nothing
        returning id
        """,
        login,
        user.get("id"),
        user.get("name"),
        user.get("location"),
    )
    if row is None:
        cid = await conn.fetchval(
            "select id from candidates where lower(login) = lower($1)", login
        )
        return int(cid), False
    cid = int(row["id"])
    # Compute the source in Python: a SQL `case when $2 is null` leaves
    # asyncpg unable to type a NULL parameter (AmbiguousParameterError).
    company = (user.get("company") or "").strip().lstrip("@") or None
    await conn.execute(
        "insert into candidate_signals (candidate_id, employer, employer_source) "
        "values ($1, $2::text, $3::text) "
        "on conflict (candidate_id) do nothing",
        cid,
        company,
        "company" if company else None,
    )
    return cid, True


async def _store_github_emails(conn, candidate_id: int, login: str, emails: dict) -> int:
    written = 0
    for email, agg in emails.items():
        if is_placeholder(email):
            continue
        occurrences = int(agg.get("occurrences") or 1)
        last_date = agg.get("last_date") or ""
        when = last_date[:10] if last_date else "unknown date"
        await conn.execute(
            UPSERT_EMAIL,
            candidate_id,
            email,
            "commit",
            is_work_domain(email),
            75,
            False,
            f"name-bridge commit search author:{login} ({occurrences} commit(s), last {when})",
            occurrences,
        )
        written += 1
    return written


_TARGETS_SQL = f"""
select c.id, c.name, c.linkedin_slug, c.location_raw,
       s.employer, s.location_metro
from candidates c
join candidate_signals s on s.candidate_id = c.id
where c.source = 'linkedin'
  and c.merged_into is null
  and c.name is not null
  and c.name like '% %'
  and s.title ~* '{ENGINEER_TITLE_SQL}'
  and (c.name_bridge_checked_at is null
       or c.name_bridge_checked_at < now() - interval '180 days')
order by c.id
"""


async def run_name_bridge(
    *,
    limit: int = 0,
    concurrency: int = 2,
    dry_run: bool = False,
) -> int:
    settings.require("github_token")
    pool = await get_pool()
    async with pool.acquire() as conn:
        targets = await conn.fetch(
            _TARGETS_SQL + (" limit $1" if limit else ""),
            *([limit] if limit else []),
        )
        if dry_run:
            printable = 0
            for row in targets:
                if name_search_query(row["name"]):
                    printable += 1
            print(
                f"name-bridge would search {printable} of {len(targets)} "
                "engineer-titled LinkedIn rows (dry run)"
            )
            return 0

        search_limiter = RateLimiter(settings.github_rpm)
        rest_limiter = RateLimiter(settings.github_rest_rpm)
        semaphore = asyncio.Semaphore(max(1, concurrency))
        queued = 0
        minted = 0
        checked = 0
        min_commits = settings.name_bridge_min_commits

        async def one(client: httpx.AsyncClient, row) -> tuple:
            async with semaphore:
                query = name_search_query(row["name"])
                if query is None:
                    return row, [], None
                try:
                    items = await _search_name(client, search_limiter, query)
                    by_login = collapse_hits_by_login(items)
                    sole = len(by_login) == 1
                    accepted: list[tuple[str, dict, dict, list[str]]] = []
                    for login, bucket in by_login.items():
                        user = await fetch_github_user(client, rest_limiter, login)
                        if not user:
                            continue
                        reasons = corroborate(
                            login=login,
                            bucket=bucket,
                            user=user,
                            linkedin=row,
                            sole_login=sole,
                            min_commits=min_commits,
                        )
                        if reasons:
                            accepted.append((login, bucket, user, reasons))
                    return row, accepted, None
                except Exception as exc:  # noqa: BLE001 - recorded per-row, never fatal
                    return row, [], exc

        async with httpx.AsyncClient(
            timeout=30.0,
            headers={
                **_SEARCH_HEADERS,
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
            },
        ) as client:
            with Heartbeat(
                log, lambda: {"checked": checked, "queued": queued, "minted": minted}
            ):
                tasks = [asyncio.ensure_future(one(client, r)) for r in targets]
                for coro in asyncio.as_completed(tasks):
                    row, accepted, exc = await coro
                    checked += 1
                    if exc is not None:
                        await conn.execute(
                            "insert into enrich_errors (candidate_id, login, stage, error) "
                            "values ($1, $2, 'name_bridge', $3)",
                            row["id"],
                            f"li:{row['linkedin_slug']}" if row["linkedin_slug"] else "",
                            str(exc)[:500],
                        )
                        continue
                    for login, bucket, user, reasons in accepted:
                        github_cid, created = await ensure_github_candidate(conn, user)
                        if created:
                            minted += 1
                        await _store_github_emails(conn, github_cid, login, bucket["emails"])
                        await queue_pair(
                            conn,
                            primary_id=github_cid,
                            merged_id=row["id"],
                            tier="commit_name",
                            evidence={
                                "login": login,
                                "reasons": reasons,
                                "commits": bucket["commits"],
                                "name": row["name"],
                            },
                        )
                        queued += 1
                    await conn.execute(
                        "update candidates set name_bridge_checked_at = now() where id = $1",
                        row["id"],
                    )

        log.info("name_bridge.done", checked=checked, queued=queued, minted=minted)
        print(
            f"name-bridge: checked {checked} LinkedIn rows, "
            f"queued {queued} review merges, minted {minted} GitHub identities"
        )
        return queued


async def amain(args: argparse.Namespace) -> int:
    try:
        await run_name_bridge(
            limit=args.limit,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
    finally:
        await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Queue review-only GitHub merges for engineer-titled LinkedIn rows."
    )
    ap.add_argument("--limit", type=int, default=0, help="max LinkedIn rows (0 = all)")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
