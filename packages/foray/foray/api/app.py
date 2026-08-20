"""Foray search API (FastAPI + asyncpg).

No external API call in the request path on a warm parse cache: archetype scores
and highlights are precomputed, and query embedding is a local model.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..db import close_pool, get_pool
from .search import run_search

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Foray", version="0.1.0")


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=0, ge=0, le=5000)
    offset: int = Field(default=0, ge=0, le=100_000)
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


@app.get("/stats")
async def stats() -> dict:
    from ..status import collect, recent_runs

    pool = await get_pool()
    async with pool.acquire() as conn:
        counts = await collect(conn)
        runs = await recent_runs(conn, 8)
    return {"counts": counts, "runs": [{k: str(v) for k, v in r.items()} for r in runs]}


@app.post("/search")
async def search(body: SearchBody) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await run_search(
            conn,
            body.query,
            body.limit,
            offset=body.offset,
            has_contact=body.has_contact,
        )
    return result.to_dict()


@app.get("/search/stream")
async def search_stream(
    q: str,
    limit: int = Query(default=0, ge=0, le=5000),
    offset: int = Query(default=0, ge=0, le=100_000),
    has_contact: bool = False,
) -> StreamingResponse:
    async def events():
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await run_search(
                conn, q, limit, offset=offset, has_contact=has_contact
            )
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
    import functools

    from ..enrich.apollo import match_and_store
    from ..enrich.linkedin import resolve_and_store

    settings.require("apollo_api_key")
    pool = await get_pool()
    async with pool.acquire() as conn:
        candidate_id = await conn.fetchval(
            "insert into shortlist_events (candidate_id, reason) "
            "select id, $2 from candidates where login = $1 "
            "returning candidate_id",
            login,
            body.reason,
        )
        if candidate_id is None:
            return {"login": login, "linkedin_url": None, "linkedin_source": None, "found": False}
        result = await resolve_and_store(
            conn,
            login,
            allow_apollo=True,
            apollo_reason=body.reason,
            apollo=functools.partial(match_and_store, conn, candidate_id),
        )
    return result


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
