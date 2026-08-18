"""On-click freshness check: one GraphQL call for a single candidate.

This is what makes the global refresh cadence almost irrelevant -- the index can
be weeks stale while every candidate a human actually reads is current to the
second. If the employer changed, we update the row and surface a badge.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from ..config import settings
from ..log import get_logger
from .employer import infer_employer

log = get_logger("enrich.freshness")

FRESHNESS_QUERY = """
query($login: String!) {
  user(login: $login) {
    login company location email
    organizations(first: 10) { nodes { login name } }
    repositories(first: 5, ownerAffiliations: OWNER, isFork: false,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes { name pushedAt stargazerCount }
    }
    contributionsCollection {
      totalCommitContributions totalPullRequestContributions
    }
  }
}
"""


async def _default_fetch_node(login: str) -> dict | None:
    import httpx

    settings.require("github_token")
    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {settings.github_token}",
            "User-Agent": "foray-freshness",
        },
        timeout=10.0,
    ) as client:
        resp = await client.post(
            "https://api.github.com/graphql",
            json={"query": FRESHNESS_QUERY, "variables": {"login": login}},
        )
        resp.raise_for_status()
        return (resp.json().get("data") or {}).get("user")


async def freshness_check(
    conn,
    login: str,
    *,
    fetch_node: Callable[[str], Awaitable[dict | None]] | None = None,
) -> dict[str, Any]:
    """Re-fetch one candidate; if the employer changed, update and return a badge."""
    fetch_node = fetch_node or _default_fetch_node
    node = await fetch_node(login)
    if node is None:
        return {"login": login, "changed": False, "found": False}

    new_employer, new_source = infer_employer(node)
    row = await conn.fetchrow(
        "select c.id, s.employer from candidates c "
        "left join candidate_signals s on s.candidate_id = c.id where c.login = $1",
        login,
    )
    if row is None:
        return {"login": login, "changed": False, "found": False}

    previous = row["employer"]
    changed = new_employer is not None and new_employer != previous

    if changed:
        await conn.execute(
            "update candidate_signals set employer = $1, employer_source = $2 where candidate_id = $3",
            new_employer,
            new_source,
            row["id"],
        )
        log.info("freshness.employer_changed", login=login, previous=previous, current=new_employer)
    await conn.execute(
        "update candidates set freshness_at = now() where id = $1", row["id"]
    )

    badge = None
    if changed:
        badge = (
            f"employer updated just now"
            + (f", previously {previous}" if previous else "")
        )
    return {
        "login": login,
        "found": True,
        "changed": changed,
        "previous": previous,
        "current": new_employer,
        "badge": badge,
    }
