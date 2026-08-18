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
        result = await run_search(conn, body.query, body.limit or None)
    return result.to_dict()


@app.get("/search/stream")
async def search_stream(q: str, limit: int = 0) -> StreamingResponse:
    async def events():
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await run_search(conn, q, limit or None)
        # First event: the ranked meta + list (this is the sub-second first byte).
        meta = result.to_dict()
        candidates = meta.pop("results")
        yield _sse("meta", meta)
        # Then one detail event per candidate (Phase 6 will attach live freshness here).
        for candidate in candidates:
            yield _sse("candidate", candidate)
        yield _sse("done", {"returned": len(candidates)})

    return StreamingResponse(events(), media_type="text/event-stream")


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
