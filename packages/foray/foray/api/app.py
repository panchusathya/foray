"""Foray search API (FastAPI + asyncpg).

No external API call in the request path on a warm parse cache: archetype scores
and highlights are precomputed, and query embedding is a local model.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..db import close_pool, get_pool
from ..search_doc import HAS_LEXICAL_DOC_SQL
from .internal import router as internal_router
from .search import probe_pgvector_version, run_search

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Foray", version="0.1.0")

# Paid routes, behind a shared secret. Everything else on this app is free
# and unauthenticated, which is why the split exists.
app.include_router(internal_router)


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
        candidates = await conn.fetchval(
            "select count(*) from candidates where embedding is not null"
        )
        scored = await conn.fetchval("select count(distinct candidate_id) from archetype_scores")
        lexical = await conn.fetchval(
            # HAS_LEXICAL_DOC_SQL, not a bare `is not null`: an empty tsvector
            # can never match `@@`, so counting it here would report a healthy
            # lexical arm over a corpus it cannot actually find. The constant
            # assumes the `s` alias (single source in search_doc), hence the
            # alias on the table.
            f"select count(*) from candidate_signals s where {HAS_LEXICAL_DOC_SQL}"
        )
        pgvector_version = await probe_pgvector_version(conn)
    status = "ok"
    if "lexical" in (settings.retrieval_arms or "") and not lexical:
        status = "degraded"
    return {
        "status": status,
        "version": app.version,
        "embedded_candidates": candidates,
        "scored_candidates": scored,
        "lexical_indexed": lexical,
        "embedding_model": settings.embedding_model,
        "retrieval_arms": settings.retrieval_arms,
        "pgvector_version": pgvector_version,
        "rerank_enabled": settings.rerank_enabled,
        "rerank_model": settings.rerank_model if settings.rerank_enabled else None,
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
        # First event: the ranked meta + list. Cross-encoder rerank (when
        # enabled) runs before this byte, so first-byte is no longer sub-second
        # on a cold CE cache; page 2+ is.
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

    if not settings.apollo_api_key:
        # settings.require() raises SystemExit, which inside a request
        # handler kills the worker rather than answering the caller.
        raise HTTPException(
            status_code=503, detail="APOLLO_API_KEY is not configured on this deployment"
        )
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


@app.on_event("startup")
async def _startup() -> None:
    from ..log import get_logger

    log = get_logger("api.app")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            lexical = await conn.fetchval(
                # Same definition as /health: rows the lexical arm can match,
                # not rows the backfill has merely visited (see search_doc).
                f"select count(*) from candidate_signals s where {HAS_LEXICAL_DOC_SQL}"
            )
        if "lexical" in (settings.retrieval_arms or "") and not lexical:
            log.warning("search.lexical_unindexed", arms=settings.retrieval_arms)
    except Exception as exc:  # noqa: BLE001 - never fail startup for a count
        log.warning("search.health_probe_failed", error=str(exc))
    if settings.rerank_enabled and settings.rerank_warmup:
        try:
            from ..rerank.model import get_cross_encoder

            get_cross_encoder()
        except Exception as exc:  # noqa: BLE001 - never fail startup for a missing model
            log.warning("rerank.warmup_failed", error=str(exc))


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()


def main() -> int:
    import uvicorn

    uvicorn.run("foray.api.app:app", host="0.0.0.0", port=8100, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
