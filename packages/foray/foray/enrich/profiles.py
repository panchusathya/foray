"""Fetch profile + repo facts via the GitHub GraphQL API, batched.

GraphQL is the single biggest speedup available: REST /users/{login} is one
user per request against 5,000/hour; a GraphQL query with ~40 aliased user
nodes costs a handful of points, so we fetch 40 users per request. The raw
response is stored per candidate in raw_profiles; signals are derived separately
(derive.py) so logic can change without refetching.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import settings
from ..db import close_pool, get_pool
from ..log import Heartbeat, get_logger

log = get_logger("enrich.profiles")

GITHUB_GRAPHQL = "https://api.github.com/graphql"
BATCH_SIZE = 40
STALE_AFTER = "90 days"

# GitHub answers batched GraphQL with occasional 502s, and its secondary rate
# limiter with 403/429 — all transient. A long harvest must ride these out.
TRANSIENT_STATUSES = {403, 429, 500, 502, 503, 504}
MAX_TRANSIENT_RETRIES = 6

_FRAGMENT = """
fragment F on User {
  name login email company location bio websiteUrl twitterUsername createdAt
  followers { totalCount }
  following { totalCount }
  repositories(first: 30, ownerAffiliations: OWNER, isFork: false,
               orderBy: {field: STARGAZERS, direction: DESC}) {
    nodes {
      name description stargazerCount forkCount pushedAt createdAt isArchived
      primaryLanguage { name }
      repositoryTopics(first: 10) { nodes { topic { name } } }
      languages(first: 5) { edges { size node { name } } }
    }
  }
  organizations(first: 10) { nodes { login name } }
  contributionsCollection {
    totalCommitContributions totalPullRequestContributions restrictedContributionsCount
  }
}
"""


def build_query(n: int) -> str:
    params = ", ".join(f"$l{i}: String!" for i in range(n))
    nodes = "\n".join(f"  u{i}: user(login: $l{i}) {{ ...F }}" for i in range(n))
    return (
        f"query({params}) {{\n"
        f"  rateLimit {{ remaining resetAt cost }}\n"
        f"{nodes}\n"
        f"}}\n{_FRAGMENT}"
    )


class GitHubGraphQL:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "foray-enricher",
            },
            transport=transport,
            timeout=60.0,
        )
        self.request_count = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _sleep_until(self, reset_at: str | None) -> None:
        if not reset_at:
            await asyncio.sleep(5)
            return
        reset = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
        delay = max(1.0, (reset - datetime.now(timezone.utc)).total_seconds() + 5)
        log.info("graphql.rate_limited", reset_at=reset_at, sleep=round(delay, 1))
        await asyncio.sleep(delay)

    async def _post_with_retry(self, body: dict) -> httpx.Response:
        """POST, riding out transient statuses with backoff (Retry-After wins)."""
        delay = 2.0
        for attempt in range(MAX_TRANSIENT_RETRIES):
            self.request_count += 1
            resp = await self._client.post(GITHUB_GRAPHQL, json=body)
            if resp.status_code not in TRANSIENT_STATUSES:
                resp.raise_for_status()
                return resp
            retry_after = resp.headers.get("retry-after")
            sleep = float(retry_after) if retry_after else delay
            log.info(
                "graphql.transient",
                status=resp.status_code,
                attempt=attempt + 1,
                sleep=round(sleep, 1),
            )
            await asyncio.sleep(min(sleep, 120.0))
            delay = min(delay * 2, 90.0)
        resp.raise_for_status()  # out of retries: surface the last status
        return resp

    async def fetch_batch(self, logins: list[str]) -> dict[str, dict | None]:
        query = build_query(len(logins))
        variables = {f"l{i}": login for i, login in enumerate(logins)}
        while True:
            resp = await self._post_with_retry({"query": query, "variables": variables})
            payload = resp.json()
            errors = payload.get("errors") or []
            data = payload.get("data")

            if (not data) and any(e.get("type") == "RATE_LIMITED" for e in errors):
                await self._sleep_until(None)
                continue
            if not data:
                raise RuntimeError(f"GraphQL returned no data: {errors}")

            result: dict[str, dict | None] = {}
            for i, login in enumerate(logins):
                result[login] = data.get(f"u{i}")

            rate = data.get("rateLimit") or {}
            remaining = rate.get("remaining")
            if isinstance(remaining, int) and remaining < 50:
                await self._sleep_until(rate.get("resetAt"))
            return result


async def _candidates_to_enrich(conn, limit: int, force: bool) -> list[tuple[int, str]]:
    rows = await conn.fetch(
        f"""
        select c.id, c.login
        from candidates c
        left join raw_profiles r on r.candidate_id = c.id
        where r.candidate_id is null
           or $1
           or r.fetched_at < now() - interval '{STALE_AFTER}'
        order by c.id
        {'limit $2' if limit else ''}
        """,
        force,
        *([limit] if limit else []),
    )
    return [(r["id"], r["login"]) for r in rows]


async def amain(args: argparse.Namespace) -> int:
    settings.require("github_token")
    pool = await get_pool()
    gh = GitHubGraphQL(settings.github_token)
    stored = 0
    missing = 0
    try:
        async with pool.acquire() as conn:
            todo = await _candidates_to_enrich(conn, args.limit, args.force)
            id_by_login = {login: cid for cid, login in todo}
            logins = [login for _cid, login in todo]

            with Heartbeat(log, lambda: {"stored": stored, "requests": gh.request_count}):
                for start in range(0, len(logins), args.batch_size):
                    chunk = logins[start : start + args.batch_size]
                    nodes = await gh.fetch_batch(chunk)
                    for login, node in nodes.items():
                        cid = id_by_login[login]
                        if node is None:
                            missing += 1
                            await conn.execute(
                                "insert into enrich_errors (candidate_id, login, stage, error) "
                                "values ($1, $2, 'fetch', 'user not found or suspended')",
                                cid,
                                login,
                            )
                            continue
                        await conn.execute(
                            "insert into raw_profiles (candidate_id, login, data, fetched_at) "
                            "values ($1, $2, $3::jsonb, now()) "
                            "on conflict (candidate_id) do update set "
                            "data = excluded.data, login = excluded.login, fetched_at = now()",
                            cid,
                            login,
                            json.dumps(node),
                        )
                        stored += 1
        log.info("profiles.done", stored=stored, missing=missing, requests=gh.request_count)
        print(
            f"fetched {stored} profiles ({missing} missing) in {gh.request_count} requests"
        )
    finally:
        await gh.aclose()
        await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch GitHub profiles+repos via batched GraphQL.")
    ap.add_argument("--limit", type=int, default=0, help="max candidates to enrich (0 = all due)")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="logins per GraphQL request")
    ap.add_argument("--force", action="store_true", help="re-fetch even if fetched within 90 days")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
