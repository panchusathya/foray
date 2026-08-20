from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from foray.api.parse import ParsedFilter, heuristic_parse
from foray.api.search import Params, _filter_clauses, run_search
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


def test_heuristic_parse_picks_vlm_vocabulary():
    parsed = heuristic_parse("founding ML engineer who has trained LLMs and VLMs")
    assert parsed.archetype_id == "founding_ml"
    vlm = heuristic_parse("multimodal VLM engineer, vision-language models")
    assert vlm.archetype_id == "founding_ml"
    research = heuristic_parse("VLM research scientist")
    assert research.archetype_id == "ml_research"


def test_build_filters_placeholder_numbering():
    parsed = ParsedFilter(metros=["SF"], languages=["go"], experience_min=5)
    params = Params()
    where = _filter_clauses(parsed, params)
    assert params.add is not None
    assert len(params.values) == 3
    assert "$1" in where and "$2" in where and "$3" in where
    assert where.index("$1") < where.index("$2") < where.index("$3")


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
            if r["score"] is not None:
                assert r["score"] >= result.display_floor  # scored rows stay above the floor
            assert r["highlights"]  # evidence templates or stored phrases
            if r["source"] != "linkedin":
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
        # Scored rows never fall below the display floor. Unscored rows may
        # appear (score is None) once retrieval is no longer gated on score.
        assert all(
            r["score"] is None or r["score"] >= result.display_floor for r in result.results
        )
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


@requires_db
async def test_lexical_arm_finds_a_term_the_vector_arm_misses(monkeypatch):
    """Acceptance: a rare literal in search_tsv surfaces on page 1 even when
    decoys have a closer embedding. If this fails, the lexical arm is dead."""
    import foray.api.search as search_module
    from foray.embed.hashing import hashing_embed
    from foray.search_doc import TS_CONFIG

    far = hashing_embed("unrelated decoy document about rust web servers")
    close = hashing_embed("founding ml engineer trained models")
    monkeypatch.setattr(search_module, "embed_query", lambda _text: close)

    prefix = f"lex-{uuid4().hex}-"
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    transaction = conn.transaction()
    await transaction.start()
    try:
        decoy_vec = "[" + ",".join(f"{x:.6f}" for x in close) + "]"
        rare_vec = "[" + ",".join(f"{x:.6f}" for x in far) + "]"
        rare_id = await conn.fetchval(
            """
            insert into candidates (login, name, embedding, embedding_model)
            values ($1, 'Rare VLLM', $2::halfvec, 'lex-test')
            returning id
            """,
            prefix + "rare",
            rare_vec,
        )
        decoy_ids = await conn.fetch(
            """
            insert into candidates (login, name, embedding, embedding_model)
            select $1 || n::text, 'Decoy ' || n::text, $2::halfvec, 'lex-test'
            from generate_series(1, 8) n
            returning id
            """,
            prefix + "decoy",
            decoy_vec,
        )
        await conn.execute(
            """
            insert into candidate_signals (
                candidate_id, primary_language, est_experience_years,
                activity_at, last_push_at, domain_markers
            )
            select $1, 'python', 6, now(), now(), array['ml']::text[]
            """,
            rare_id,
        )
        await conn.execute(
            f"""
            update candidate_signals
               set search_tsv = to_tsvector('{TS_CONFIG}'::regconfig, 'vllm inference runtime')
             where candidate_id = $1
            """,
            rare_id,
        )
        await conn.execute(
            """
            insert into candidate_signals (
                candidate_id, primary_language, est_experience_years,
                activity_at, last_push_at, domain_markers
            )
            select id, 'python', 6, now(), now(), array['ml']::text[]
            from unnest($1::bigint[]) id
            """,
            [int(r["id"]) for r in decoy_ids],
        )
        result = await run_search(
            conn, "founding ML engineer vllm", 10, use_llm=False
        )
        logins = [row["login"] for row in result.results]
        assert prefix + "rare" in logins
        assert result.results[0]["login"] == prefix + "rare" or prefix + "rare" in logins[:3]
    finally:
        await transaction.rollback()
        await conn.close()


@requires_db
async def test_search_never_raises_on_adversarial_query():
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        for raw in ("&|!:*", "(unbalanced", '""', "a" * 4000, "🚀", ""):
            if not raw:
                continue
            result = await run_search(conn, raw, 5, use_llm=False)
            assert result.returned >= 0
    finally:
        await conn.close()


@requires_db
async def test_ef_search_does_not_leak_from_the_pool():
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        await run_search(conn, "backend engineer rust", 5, use_llm=False)
        value = await conn.fetchval("show hnsw.ef_search")
        # Default pgvector ef_search is 40. SET LOCAL must not stick.
        assert str(value) in {"40", "20"} or int(value) <= 40
    finally:
        await conn.close()


@requires_db
async def test_search_tsv_is_written_by_derive():
    from foray.enrich.derive import derive_signals

    prefix = f"tsv-{uuid4().hex}-"
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    transaction = conn.transaction()
    await transaction.start()
    try:
        cid = await conn.fetchval(
            """
            insert into candidates (login, name, source)
            values ($1, 'Tsv Person', 'github')
            returning id
            """,
            prefix,
        )
        node = {
            "name": "Tsv Person",
            "login": prefix,
            "bio": "vllm inference",
            "createdAt": "2018-01-01T00:00:00Z",
            "repositories": {"nodes": []},
            "contributionsCollection": {
                "totalCommitContributions": 1,
                "totalPullRequestContributions": 0,
                "restrictedContributionsCount": 0,
            },
        }
        await conn.execute(
            """
            insert into raw_profiles (candidate_id, source, login, data)
            values ($1, 'github', $2, $3::jsonb)
            """,
            cid,
            prefix,
            __import__("json").dumps(node),
        )
        from foray.enrich.derive import _upsert_signals
        from foray.search_doc import UPDATE_SEARCH_TSV_SQL

        signals = derive_signals(node)
        await _upsert_signals(conn, cid, node, signals)
        await conn.execute(UPDATE_SEARCH_TSV_SQL, [cid])
        tsv = await conn.fetchval(
            "select search_tsv is not null from candidate_signals where candidate_id = $1",
            cid,
        )
        assert tsv is True
    finally:
        await transaction.rollback()
        await conn.close()


@requires_db
async def test_vector_arm_uses_hnsw_index():
    """If this fails, read the plan; do not delete the test.

    Silently losing the HNSW scan to a sequential scan is the classic pgvector
    regression — it does not show up as a wrong answer, and a p95 number will
    not localise it. ``enable_seqscan=off`` makes the assertion about index
    usability rather than planner cost estimates on a tiny fixture table.
    """
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        exists = await conn.fetchval(
            "select 1 from pg_indexes where indexname = 'ix_candidates_embedding'"
        )
        if not exists:
            pytest.skip("ix_candidates_embedding is created by embed/run.py, not migrate")
        dummy = "[" + ",".join(["0"] * 384) + "]"
        async with conn.transaction():
            await conn.execute("select set_config('enable_seqscan', 'off', true)")
            plan = await conn.fetch(
                """
                explain
                select c.id
                  from candidates c
                  join candidate_signals s on s.candidate_id = c.id
                 where c.merged_into is null and c.embedding is not null
                 order by c.embedding <=> $1::halfvec, c.id
                 limit 50
                """,
                dummy,
            )
        blob = "\n".join(row[0] for row in plan)
        assert "ix_candidates_embedding" in blob
    finally:
        await conn.close()


@requires_db
async def test_lexical_arm_uses_gin_index():
    """If this fails, read the plan; do not delete the test."""
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        exists = await conn.fetchval(
            "select 1 from pg_indexes where indexname = 'ix_signals_search_tsv'"
        )
        if not exists:
            pytest.skip("ix_signals_search_tsv missing — run migrations/006")
        async with conn.transaction():
            await conn.execute("select set_config('enable_seqscan', 'off', true)")
            plan = await conn.fetch(
                """
                explain
                select c.id
                  from candidates c
                  join candidate_signals s on s.candidate_id = c.id
                 where c.merged_into is null
                   and c.embedding is not null
                   and s.search_tsv @@ websearch_to_tsquery('english'::regconfig, 'vllm')
                 order by ts_rank_cd('{0.1,0.2,0.4,1.0}'::real[], s.search_tsv,
                           websearch_to_tsquery('english'::regconfig, 'vllm'), 32) desc, c.id
                 limit 50
                """
            )
        blob = "\n".join(row[0] for row in plan)
        assert "ix_signals_search_tsv" in blob
    finally:
        await conn.close()

