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
from datetime import UTC, datetime

import httpx

from ..config import settings
from ..db import close_pool, get_pool
from ..log import Heartbeat, get_logger
from ._http import request_with_retry

log = get_logger("enrich.profiles")

GITHUB_GRAPHQL = "https://api.github.com/graphql"
# 40 aliased users x 30 repos x topics x languages regularly makes GitHub time
# out server-side and silently null the heavy fields (repositories.nodes) while
# still returning 200 with partial data. 12 stays reliably under that.
BATCH_SIZE = 12
STALE_AFTER = "90 days"

# Page sizes are config-driven so the payload can be tuned on the VM without a
# rebuild. Nested node count (repos x (topics + languages)) is what makes GitHub
# time out server-side and null repositories.nodes; keep it modest. Do NOT drop
# languages below ~3: primary_language / language_mix are derived only from
# repo.languages.edges and gate several archetypes.
_FRAGMENT_TMPL = """
fragment F on User {
  name login email company location bio websiteUrl twitterUsername createdAt
  followers { totalCount }
  following { totalCount }
  repositories(first: %(repos)d, ownerAffiliations: OWNER, isFork: false,
               orderBy: {field: STARGAZERS, direction: DESC}) {
    nodes {
      name description stargazerCount forkCount pushedAt createdAt isArchived
      primaryLanguage { name }
      repositoryTopics(first: %(topics)d) { nodes { topic { name } } }
      languages(first: %(languages)d) { edges { size node { name } } }
    }
  }
  organizations(first: 10) { nodes { login name } }
  contributionsCollection {
    totalCommitContributions totalPullRequestContributions restrictedContributionsCount
  }
}
"""


def _fragment() -> str:
    return _FRAGMENT_TMPL % {
        "repos": settings.enrich_repos,
        "topics": settings.enrich_topics,
        "languages": settings.enrich_languages,
    }


def build_query(n: int) -> str:
    params = ", ".join(f"$l{i}: String!" for i in range(n))
    nodes = "\n".join(f"  u{i}: user(login: $l{i}) {{ ...F }}" for i in range(n))
    return (
        f"query({params}) {{\n"
        f"  rateLimit {{ remaining resetAt cost }}\n"
        f"{nodes}\n"
        f"}}\n{_fragment()}"
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
        delay = max(1.0, (reset - datetime.now(UTC)).total_seconds() + 5)
        log.info("graphql.rate_limited", reset_at=reset_at, sleep=round(delay, 1))
        await asyncio.sleep(delay)

    async def _post_with_retry(self, body: dict) -> httpx.Response:
        """POST, riding out transient statuses with backoff (shared policy)."""

        def _count() -> None:
            self.request_count += 1

        return await request_with_retry(
            self._client, "POST", GITHUB_GRAPHQL, log=log, on_request=_count, json=body
        )

    async def fetch_batch(self, logins: list[str]) -> dict[str, dict | None]:
        query = build_query(len(logins))
        variables = {f"l{i}": login for i, login in enumerate(logins)}
        bad_json = 0
        while True:
            resp = await self._post_with_retry({"query": query, "variables": variables})
            try:
                payload = resp.json()
            except ValueError:
                # 200 with a garbage body happens when GitHub is under stress.
                bad_json += 1
                if bad_json > 4:
                    raise RuntimeError("GraphQL kept returning unparseable bodies") from None
                log.info("graphql.bad_json", attempt=bad_json, body=resp.text[:120])
                await asyncio.sleep(5 * bad_json)
                continue
            errors = payload.get("errors") or []
            data = payload.get("data")

            if (not data) and any(e.get("type") == "RATE_LIMITED" for e in errors):
                await self._sleep_until(None)
                continue
            if not data:
                raise RuntimeError(f"GraphQL returned no data: {errors}")
            if errors:
                # Partial result: some aliases nulled (server-side timeout, etc).
                # Callers detect incomplete nodes and re-fetch them singly.
                log.info("graphql.partial", errors=len(errors), first=str(errors[0])[:200])

            result: dict[str, dict | None] = {}
            for i, login in enumerate(logins):
                result[login] = data.get(f"u{i}")

            rate = data.get("rateLimit") or {}
            remaining = rate.get("remaining")
            if isinstance(remaining, int) and remaining < 50:
                await self._sleep_until(rate.get("resetAt"))
            return result


def _complete(node: dict | None) -> bool:
    """A usable profile node: present AND with the heavy fields intact.

    GitHub returns 200 with ``repositories.nodes: null`` when the query times
    out server-side — storing that would derive zero stars/markers for a real
    user, which is worse than no row at all.
    """
    return node is not None and (node.get("repositories") or {}).get("nodes") is not None


async def _fetch_with_singles_fallback(
    gh: GitHubGraphQL, chunk: list[str]
) -> dict[str, dict | None]:
    """Batch fetch, then re-fetch any incomplete node on its own once.

    A chunk (or login) that exhausts the transient retries must not kill a
    multi-hour run: the batch degrades to singles, and a single that still
    fails is returned as None so the caller records it and moves on.
    """
    # Broad catches are deliberate: ANY per-chunk failure mode (HTTP status,
    # unparseable body, no-data payload, timeout) degrades rather than kills.
    try:
        nodes = await gh.fetch_batch(chunk)
    except Exception as exc:  # noqa: BLE001
        log.info("enrich.batch_failed", size=len(chunk), error=str(exc)[:120])
        nodes = {login: None for login in chunk}
    for login in [l for l, n in nodes.items() if not _complete(n)]:
        try:
            single = await gh.fetch_batch([login])
            nodes[login] = single.get(login)
        except Exception as exc:  # noqa: BLE001
            log.info("enrich.single_failed", login=login, error=str(exc)[:120])
            nodes[login] = None
    return nodes


async def _candidates_to_enrich(
    conn, limit: int, force: bool, retry_errors: bool
) -> list[tuple[int, str]]:
    # Exclude candidates that hit a fetch error recently. Otherwise a
    # permanently-dead login (suspended, deleted, renamed) never gets a
    # raw_profiles row, keeps its low id, and is re-selected at the FRONT of
    # this queue every night -- and once the dead set exceeds --limit, enrich
    # makes zero progress and the pipeline silently deadlocks.
    error_exclude = (
        ""
        if (force or retry_errors)
        else """
           and not exists (
               select 1 from enrich_errors e
               where e.candidate_id = c.id and e.stage = 'fetch'
                 and e.created_at > now() - interval '14 days'
           )"""
    )
    rows = await conn.fetch(
        f"""
        select c.id, c.login
        from candidates c
        left join raw_profiles r
               on r.candidate_id = c.id and r.source = 'github'
        where c.source in ('github', 'both')
          and c.merged_into is null
          and (r.candidate_id is null
           or $1
           or r.fetched_at < now() - interval '{STALE_AFTER}')
        {error_exclude}
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
            todo = await _candidates_to_enrich(
                conn, args.limit, args.force, args.retry_errors
            )
            id_by_login = {login: cid for cid, login in todo}
            logins = [login for _cid, login in todo]

            with Heartbeat(log, lambda: {"stored": stored, "requests": gh.request_count}):
                for start in range(0, len(logins), args.batch_size):
                    chunk = logins[start : start + args.batch_size]
                    nodes = await _fetch_with_singles_fallback(gh, chunk)
                    for login, node in nodes.items():
                        cid = id_by_login[login]
                        if not _complete(node):
                            missing += 1
                            await conn.execute(
                                "insert into enrich_errors (candidate_id, login, stage, error) "
                                "values ($1, $2, 'fetch', $3)",
                                cid,
                                login,
                                "user not found or suspended"
                                if node is None
                                else "incomplete node after single retry",
                            )
                            continue
                        await conn.execute(
                            "insert into raw_profiles (candidate_id, source, login, data, fetched_at) "
                            "values ($1, 'github', $2, $3::jsonb, now()) "
                            "on conflict (candidate_id, source) do update set "
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
    ap.add_argument(
        "--batch-size",
        type=int,
        default=settings.enrich_batch_size,
        help="logins per GraphQL request",
    )
    ap.add_argument("--force", action="store_true", help="re-fetch even if fetched within 90 days")
    ap.add_argument(
        "--retry-errors",
        action="store_true",
        help="include logins that recently failed to fetch (default excludes them)",
    )
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
