"""Foray search API (FastAPI + asyncpg).

No external API call in the request path on a warm parse cache: archetype scores
and highlights are precomputed, and query embedding is a local model.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..db import close_pool, get_pool
from .search import run_search

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Foray", version="0.1.0")


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=0, ge=0, le=200)
    # Only candidates with an observed email or a resolved LinkedIn. Applied in
    # the SQL filters, so total_matched reflects the real contactable pool.
    has_contact: bool = False


@app.get("/health")
async def health() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        candidates = await conn.fetchval("select count(*) from candidates where embedding is not null")
        scored = await conn.fetchval("select count(distinct candidate_id) from archetype_scores")
    return {
        "status": "ok",
        "version": app.version,
        "embedded_candidates": candidates,
        "scored_candidates": scored,
        "embedding_model": settings.embedding_model,
    }


@app.post("/search")
async def search(body: SearchBody) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await run_search(
            conn, body.query, body.limit or None, has_contact=body.has_contact
        )
    return result.to_dict()


@app.get("/search/stream")
async def search_stream(q: str, limit: int = 0, has_contact: bool = False) -> StreamingResponse:
    async def events():
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await run_search(conn, q, limit or None, has_contact=has_contact)
        # First event: the ranked meta + list (this is the sub-second first byte).
        meta = result.to_dict()
        candidates = meta.pop("results")
        yield _sse("meta", meta)
        # Then one detail event per candidate (Phase 6 will attach live freshness here).
        for candidate in candidates:
            yield _sse("candidate", candidate)
        yield _sse("done", {"returned": len(candidates)})

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/candidate/{login}/enrich")
async def enrich_on_click(login: str) -> dict:
    """Fired when a human opens a candidate: lazy LinkedIn (no Apollo) + freshness."""
    from ..enrich.freshness import freshness_check
    from ..enrich.linkedin import resolve_and_store

    pool = await get_pool()
    async with pool.acquire() as conn:
        linkedin = await resolve_and_store(conn, login, allow_apollo=False)
        freshness = None
        if settings.github_token:
            try:
                freshness = await freshness_check(conn, login)
            except Exception as exc:  # noqa: BLE001 - freshness is best-effort
                freshness = {"error": str(exc)}
    return {"login": login, "linkedin": linkedin, "freshness": freshness}


class ApolloBody(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


@app.post("/candidate/{login}/apollo")
async def apollo_resolve(login: str, body: ApolloBody) -> dict:
    """Explicit shortlist action: allow the Apollo step (a credit). Requires a reason."""
    from ..enrich.linkedin import resolve_and_store

    settings.require("apollo_api_key")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "insert into shortlist_events (candidate_id, reason) "
            "select id, $2 from candidates where login = $1",
            login,
            body.reason,
        )
        result = await resolve_and_store(
            conn, login, allow_apollo=True, apollo_reason=body.reason, apollo=_apollo_people_match
        )
    return result


async def _apollo_people_match(node: dict) -> str | None:
    """Apollo people-match by name + employer domain (only reached in shortlist context)."""
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.apollo.io/v1/people/match",
            headers={"X-Api-Key": settings.apollo_api_key, "Content-Type": "application/json"},
            json={"name": node.get("name"), "organization_name": node.get("company")},
        )
        if resp.status_code != 200:
            return None
        person = (resp.json() or {}).get("person") or {}
        return person.get("linkedin_url")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()


def main() -> int:
    import uvicorn

    uvicorn.run("foray.api.app:app", host="0.0.0.0", port=8100, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
