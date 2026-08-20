from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg

from foray.api.parse import ParsedFilter, heuristic_parse
from foray.api.search import _build_filters, run_search
from foray.config import settings
from foray.db import _asyncpg_dsn
from tests.dbutil import requires_db, requires_scored_db

# ------------------------------------------------------------------- offline --


def test_heuristic_parse_picks_archetype_and_extracts_filters():
    parsed = heuristic_parse("founding ML engineer in San Francisco with python, 3-8 years")
    assert parsed.archetype_id == "founding_ml"
    assert "San Francisco" in parsed.metros
    assert "python" in parsed.languages
    assert parsed.experience_min == 3 and parsed.experience_max == 8


def test_build_filters_placeholder_numbering():
    parsed = ParsedFilter(metros=["SF"], languages=["go"], experience_min=5)
    where, params, next_i = _build_filters(parsed, start=3)
    assert "$3" in where and "$4" in where and "$5" in where
    assert next_i == 6
    assert len(params) == 3


def test_frontend_never_renders_score():
    html = (Path(__file__).resolve().parent.parent / "foray/api/static/index.html").read_text()
    # The numeric score is kept in the payload but must never be rendered.
    assert "c.score" not in html
    assert "hit.score" not in html
    assert ".score" not in html.replace(".sub", "").replace("class=\"score", "")


# ------------------------------------------------------------------ DB tests --


@requires_scored_db
async def test_search_returns_scored_results_above_floor():
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        result = await run_search(conn, "founding ML engineer, shipped training infra", 40)
        assert result.archetype_id == "founding_ml"
        assert result.returned == len(result.results)
        assert result.returned <= 40
        for r in result.results:
            assert r["score"] >= result.display_floor  # nothing below the floor
            assert r["highlights"]  # precomputed, nothing to wait for
            assert r["github_url"] == f"https://github.com/{r['login']}"
            assert r["inferred"]["est_experience_years"] is True
    finally:
        await conn.close()


@requires_scored_db
async def test_never_pads_below_floor():
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        # A very high experience floor should shrink the eligible pool.
        result = await run_search(
            conn, "senior backend engineer with 40-60 years experience", 40
        )
        # No matter what, we never return anyone below the display floor.
        assert all(r["score"] >= result.display_floor for r in result.results)
        if result.total_matched < settings.min_results_before_widen:
            assert "Widen" in (result.message or "")
    finally:
        await conn.close()


@requires_scored_db
async def test_vector_fallback_when_no_archetype():
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        result = await run_search(conn, "someone who likes long walks on the beach", 10)
        assert result.parser == "vector-fallback"
        assert result.archetype_id is None
        for r in result.results:
            assert r["score"] is None  # nothing scored in a pure semantic search
    finally:
        await conn.close()


@requires_scored_db
async def test_no_external_api_in_request_path(monkeypatch):
    import httpx

    from foray.embed.model import embed_query

    # Warm the local embedding model BEFORE blocking httpx (model load is local).
    embed_query("warm up")

    def boom(*args, **kwargs):
        raise RuntimeError("no external API calls allowed in the request path")

    monkeypatch.setattr(httpx, "Client", boom)
    monkeypatch.setattr(httpx, "AsyncClient", boom)

    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        result = await run_search(conn, "infrastructure platform engineer kubernetes", 20)
        assert result.returned >= 0  # completed with zero external calls
    finally:
        await conn.close()


@requires_db
async def test_pagination_is_stable_and_uses_absolute_ranks(monkeypatch):
    """Seed a scored corpus in a rollback-only transaction so CI cannot skip paging."""
    import foray.api.search as search_module
    from foray.score.archetypes import load_archetypes

    monkeypatch.setattr(search_module, "embed_query", lambda _text: [1.0] + [0.0] * 383)
    vector = "[1," + ",".join(["0"] * 383) + "]"
    arch = load_archetypes()["senior_backend"]
    prefix = f"paging-{uuid4().hex}-"

    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    transaction = conn.transaction()
    await transaction.start()
    try:
        await conn.execute(
            """
            insert into archetypes (id, label, gate, rubric, rubric_hash)
            values ($1, $2, $3, $4, $5)
            on conflict (id) do nothing
            """,
            arch.id,
            arch.label,
            arch.gate,
            arch.rubric,
            arch.rubric_hash,
        )
        candidate_rows = await conn.fetch(
            """
            insert into candidates (login, name, embedding, embedding_model)
            select $1 || n::text, 'Paging Candidate ' || n::text,
                   $2::halfvec, 'paging-test'
            from generate_series(1, 90) n
            returning id
            """,
            prefix,
            vector,
        )
        candidate_ids = [int(row["id"]) for row in candidate_rows]
        await conn.execute(
            """
            insert into candidate_signals (
                candidate_id, primary_language, est_experience_years,
                activity_at, last_push_at
            )
            select id, 'haskell', 47, now(), now()
            from unnest($1::bigint[]) id
            """,
            candidate_ids,
        )
        await conn.execute(
            """
            insert into archetype_scores (
                candidate_id, archetype_id, score, highlights, rationale,
                flags, rubric_hash, model
            )
            select id, $2, 60, '["paging evidence"]'::jsonb, 'paging test',
                   '[]'::jsonb, $3, 'paging-test'
            from unnest($1::bigint[]) id
            """,
            candidate_ids,
            arch.id,
            arch.rubric_hash,
        )

        query = f"senior backend engineer in Haskell with 47-48 years {prefix}"
        first = await run_search(conn, query, 40, offset=0, use_llm=False)
        second = await run_search(conn, query, 40, offset=40, use_llm=False)
        repeated = await run_search(conn, query, 40, offset=0, use_llm=False)

        first_logins = [row["login"] for row in first.results]
        second_logins = [row["login"] for row in second.results]
        assert first.total_matched == first.paginable == 90
        assert first.page_count == 3 and first.has_more is True
        assert second.results[0]["rank"] == 41
        assert not set(first_logins) & set(second_logins)
        assert first_logins == [row["login"] for row in repeated.results]
    finally:
        await transaction.rollback()
        await conn.close()
