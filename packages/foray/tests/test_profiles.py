from __future__ import annotations

import json

import httpx

from foray.enrich.profiles import GitHubGraphQL, build_query


def test_build_query_has_aliases_and_fragment():
    q = build_query(3)
    assert "$l0: String!" in q and "$l2: String!" in q
    assert "u0: user(login: $l0)" in q
    assert "fragment F on User" in q
    assert "contributionsCollection" in q


async def test_fetch_400_in_under_15_requests():
    """Recording fixture: 400 logins at batch 40 must cost 10 requests (< 15)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        variables = body["variables"]
        data = {"rateLimit": {"remaining": 4000, "resetAt": "2999-01-01T00:00:00Z", "cost": 1}}
        for key, login in variables.items():
            idx = key[1:]  # "l0" -> "0"
            data[f"u{idx}"] = {
                "login": login,
                "name": login,
                "repositories": {"nodes": []},
                "organizations": {"nodes": []},
                "contributionsCollection": {
                    "totalCommitContributions": 0,
                    "totalPullRequestContributions": 0,
                },
                "createdAt": "2015-01-01T00:00:00Z",
            }
        return httpx.Response(200, json={"data": data})

    gh = GitHubGraphQL("tok", transport=httpx.MockTransport(handler))
    logins = [f"user{i}" for i in range(400)]
    seen: dict[str, dict | None] = {}
    for start in range(0, len(logins), 40):
        seen.update(await gh.fetch_batch(logins[start : start + 40]))
    await gh.aclose()

    assert len(seen) == 400
    assert gh.request_count == 10
    assert gh.request_count < 15


async def test_missing_user_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        variables = body["variables"]
        data = {"rateLimit": {"remaining": 4000, "resetAt": "2999-01-01T00:00:00Z"}}
        # Simulate a not-found user: node is null.
        for key in variables:
            data[f"u{key[1:]}"] = None
        return httpx.Response(200, json={"data": data})

    gh = GitHubGraphQL("tok", transport=httpx.MockTransport(handler))
    result = await gh.fetch_batch(["ghost"])
    await gh.aclose()
    assert result == {"ghost": None}
