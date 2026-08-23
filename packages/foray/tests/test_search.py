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
    # Canonical metro KEY, not the shard-grid string: this is what
    # candidate_signals.location_metro stores.
    assert parsed.metros == ["sf_bay_area"]
    assert "python" in parsed.languages
    assert parsed.experience_min == 3 and parsed.experience_max == 8
    # "founding" is a stage descriptor, not a claim that this person founded
    # anything. Turning it into `s.is_founder` would gate the single most
    # important query in the product down to actual founders.
    assert parsed.is_founder is None


def test_heuristic_parse_extracts_the_seniority_ladder():
    """The query floor for a word must equal the rank derive assigns to a title
    containing it. Expectations come from foray.seniority, never from literals:
    the first cut of SENIORITY_FLOOR hardcoded staff=4 / senior=3, one rung
    below derive, so every seniority query admitted an extra rung — and a test
    pinning those same literals passed the whole time."""
    from foray.seniority import parse_seniority

    for query, title in (
        ("senior backend engineer", "Senior Backend Engineer"),
        ("Sr. backend engineer", "Sr. Backend Engineer"),
        ("staff infra engineer", "Staff Infrastructure Engineer"),
        ("principal security engineer", "Principal Security Engineer"),
        ("distinguished engineer", "Distinguished Engineer"),
    ):
        floor = heuristic_parse(query).seniority_min
        derived_rank, _mgmt, _founder = parse_seniority(title)
        assert floor == derived_rank, (
            f"{query!r} floors at {floor} but derive ranks {title!r} as "
            f"{derived_rank} — a row matching the query's own words would be "
            f"{'excluded' if floor and derived_rank and floor > derived_rank else 'joined by a lower rung'}"
        )
    assert heuristic_parse("backend engineer").seniority_min is None


def test_two_seniority_words_take_the_lower_floor():
    """seniority_min is a hard predicate. Reading "senior or staff" as staff
    would silently delete every senior; over-widening is a ranking problem,
    over-narrowing is invisible."""
    from foray.seniority import SENIORITY_SENIOR

    assert heuristic_parse("senior or staff ML engineer").seniority_min == SENIORITY_SENIOR


def test_founder_nouns_set_is_founder_but_the_adjective_does_not():
    assert heuristic_parse("co-founder of a fintech startup").is_founder is True
    assert heuristic_parse("looking for founders").is_founder is True
    assert heuristic_parse("founding engineer at a seed startup").is_founder is None


def test_management_framing_is_never_seniority():
    """"Engineering Manager" is not "senior IC" — separate columns, and
    collapsing them is what makes a seniority filter useless."""
    parsed = heuristic_parse("engineering manager for the ML platform team")
    assert parsed.is_manager is True
    assert parsed.seniority_min is None
    assert heuristic_parse("VP of engineering").is_manager is True
    assert heuristic_parse("backend engineer").is_manager is None


def test_consumed_facet_words_are_stripped_from_free_text():
    """The structural version of the _NOISE guarantee: a word that became a
    predicate cannot also reach the lexical tsquery, whether or not anyone
    remembered to add it to search_doc._NOISE."""
    parsed = heuristic_parse("staff ML engineer in San Francisco, 5+ years")
    residue = parsed.free_text.lower()
    for consumed in ("staff", "san francisco", "5", "years"):
        assert consumed not in residue
    # Technology words are NOT consumed: for 96% of the corpus the lexical arm
    # is the only place a language can match at all.
    assert "ml" in residue and "engineer" in residue
    assert heuristic_parse("rust systems engineer").free_text == "rust systems engineer"


def test_noise_list_is_untouched_by_the_facet_parser():
    """Do not un-noise senior/staff/founding/shipped. Ranking "Staff
    Accountant" for "staff engineer" is the LA Fitness incident in a new
    costume; the parser consumes these words into predicates, and _NOISE stays
    the backstop for the LLM parser and for a query consumed to nothing."""
    from foray.search_doc import _NOISE

    for word in ("senior", "staff", "founding", "shipped", "principal"):
        assert word in _NOISE


def test_heuristic_parse_picks_vlm_vocabulary():
    parsed = heuristic_parse("founding ML engineer who has trained LLMs and VLMs")
    assert parsed.archetype_id == "founding_ml"
    vlm = heuristic_parse("multimodal VLM engineer, vision-language models")
    assert vlm.archetype_id == "founding_ml"
    research = heuristic_parse("VLM research scientist")
    assert research.archetype_id == "ml_research"


def _placeholders(where: str) -> list[int]:
    import re as _re

    return [int(n) for n in _re.findall(r"\$(\d+)", where)]


def test_build_filters_placeholder_numbering():
    """Every value is bound, numbering is contiguous from $1, and the first
    appearance of each placeholder is in insertion order. Asserted structurally
    rather than by count so adding a facet cannot silently renumber an arm."""
    parsed = ParsedFilter(metros=["sf_bay_area"], languages=["go"], experience_min=5)
    params = Params()
    where = _filter_clauses(parsed, params)
    numbers = _placeholders(where)
    assert numbers == sorted(set(numbers))  # each used once, ascending
    assert numbers == list(range(1, len(params.values) + 1))


def test_filter_clauses_append_the_archetype_gate():
    """Retrieval is gated on the archetype's full gate SQL: without it, any
    metro/experience match is retrievable for an ML search (the LA Fitness
    trainer bug). The gate is repo-controlled literal SQL, never a bind param."""
    from foray.score.archetypes import load_archetypes

    arch = load_archetypes()["founding_ml"]
    params = Params()
    where = _filter_clauses(ParsedFilter(), params, gate_sql=arch.gate)
    assert f"({arch.gate})" in where
    assert params.values == []  # the gate consumes no placeholders
    assert "domain_markers @> array['ml']" in where


def test_embedding_requirement_is_vector_arm_only():
    """Keyword search must not depend on the embed pipeline: the shared
    filters (count / lexical / score / enrich) no longer require an embedding,
    only the vector arm does — it is the only query that dereferences
    c.embedding, and a NULL-embedding row is retrievable via the lexical arm
    while the embed backfill catches up."""
    shared = _filter_clauses(ParsedFilter(), Params())
    assert "embedding is not null" not in shared
    vector = _filter_clauses(ParsedFilter(), Params(), require_embedding=True)
    assert "c.embedding is not null" in vector
    # The flag adds no bind parameters, so arm WHEREs stay numbering-compatible.
    plain, gated = Params(), Params()
    _filter_clauses(ParsedFilter(metros=["sf_bay_area"]), plain)
    _filter_clauses(ParsedFilter(metros=["sf_bay_area"]), gated, require_embedding=True)
    assert plain.values == gated.values


def test_recency_clause_is_the_shared_module_not_a_literal():
    """The old literal `s.activity_at > now() - interval 'N months'` was a
    no-op for 96% of the corpus: activity_at for LinkedIn rows was the import
    wall-clock, so 196,778/196,778 passed the 24-month floor. One three-valued
    predicate now, shared with all 15 archetype gates."""
    from foray.recency import recency_clause

    where = _filter_clauses(ParsedFilter(), Params())
    expected = recency_clause(
        settings.activity_floor_months, settings.snapshot_floor_months
    )
    assert expected in where
    # The unknown arm must be present: without it the ~51% of LinkedIn rows
    # with no decodable activity id silently vanish from every search.
    assert "s.activity_at is null" in where
    assert "s.profile_asof" in where
    # No bind parameters: the clause is literal SQL so arm WHEREs stay
    # $N-numbering-compatible.
    params = Params()
    _filter_clauses(ParsedFilter(), params)
    assert params.values == []


def test_search_eligible_is_in_the_shared_clause_list():
    """Admission control is one stored flag, computed by enrich/derive.py's
    admission(). It sits in the SHARED list so an inadmissible row is invisible
    to the count, the lexical arm and the vector arm alike."""
    for kwargs in ({}, {"require_embedding": True}, {"has_contact": True}):
        assert "s.search_eligible" in _filter_clauses(
            ParsedFilter(), Params(), **kwargs
        )


def test_language_filter_admits_null_by_default():
    """primary_language is GitHub-only (NULL on all 196,778 LinkedIn rows), so
    the old hard equality matched 550 of 204,535 rows — a source filter in
    disguise. Default: three-valued (match OR unknown)."""
    params = Params()
    where = _filter_clauses(ParsedFilter(languages=["Python"]), params)
    assert "(s.primary_language = any($1::text[]) or s.primary_language is null)" in where
    assert params.values == [["python"]]


def test_strict_language_restores_the_exact_predicate():
    params = Params()
    where = _filter_clauses(
        ParsedFilter(languages=["go"], strict_language=True), params
    )
    assert "s.primary_language = any($1::text[])" in where
    assert "s.primary_language is null" not in where
    assert params.values == [["go"]]


def test_experience_bounds_exclude_unknown_by_default():
    """est_experience_years is NULL for 134,536 rows. Strict is the default:
    an absence of data is not evidence of zero years, which is exactly what
    the old `default 0` storage asserted."""
    params = Params()
    where = _filter_clauses(
        ParsedFilter(experience_min=3.0, experience_max=8.0), params
    )
    assert "s.est_experience_years >= $1" in where
    assert "s.est_experience_years <= $2" in where
    # Scoped to the experience column: `is null` legitimately appears
    # elsewhere in the shared list (merged_into, and the recency clause's
    # unknown-activity arm).
    assert "s.est_experience_years is null" not in where
    assert params.values == [3.0, 8.0]


def test_include_unknown_experience_admits_nulls_on_both_bounds():
    """The max-only brief ("under 8 years") is screening OUT the
    over-experienced, so it reasonably wants the unknowns back."""
    params = Params()
    where = _filter_clauses(
        ParsedFilter(experience_max=8.0, include_unknown_experience=True), params
    )
    assert "(s.est_experience_years <= $1 or s.est_experience_years is null)" in where
    params = Params()
    where = _filter_clauses(
        ParsedFilter(
            experience_min=3.0, experience_max=8.0, include_unknown_experience=True
        ),
        params,
    )
    assert "(s.est_experience_years >= $1 or s.est_experience_years is null)" in where
    assert "(s.est_experience_years <= $2 or s.est_experience_years is null)" in where


def test_no_text_facet_uses_ilike_any():
    """The measured reason, from the plan: `ilike any(array[...])` is a
    ScalarArrayOp and CANNOT use a trigram GIN index — prod EXPLAIN showed a
    seq scan, 38k buffers, 219 ms for ONE metro predicate, and _filter_clauses
    is rebuilt for the count + vector + lexical + enrich statements. An
    OR-chain of individual LIKEs can bitmap-OR the index instead."""
    where = _filter_clauses(
        ParsedFilter(
            metros=["sf_bay_area", "nyc"],
            titles=["ml engineer"],
            title_exclude=["recruiter"],
            employers=["Google", "UCSF"],
        ),
        Params(),
    )
    assert "ilike any(" not in where
    assert where.count("lower(c.location_raw) like") >= 2
    assert "lower(s.title) like" in where


def test_metro_uses_the_canonical_key_and_the_raw_text_arm():
    """Two arms because metro evidence is stored twice: derive writes NO
    location_metro for the 7,757 GitHub rows, whose only location evidence is
    candidates.location_raw."""
    params = Params()
    where = _filter_clauses(ParsedFilter(metros=["sf_bay_area"]), params)
    assert "s.location_metro = any($1::text[])" in where
    assert params.values[0] == ["sf_bay_area"]
    assert "%palo alto%" in params.values
    # Two-character aliases never become LIKE bodies: pg_trgm cannot index a
    # pattern with under 3 characters of literal text.
    assert "%sf%" not in params.values


def test_titles_admit_unknown_by_default():
    """title is NULL on all 7,757 GitHub rows (derive has no title source for
    them), so a bare LIKE means "and is a LinkedIn row"."""
    where = _filter_clauses(ParsedFilter(titles=["ml engineer"]), Params())
    assert "(lower(s.title) like $1 or s.title is null)" in where
    strict = _filter_clauses(
        ParsedFilter(titles=["ml engineer"], strict_titles=True), Params()
    )
    assert "s.title is null" not in strict


def test_title_exclude_does_not_delete_null_titles():
    """The three-valued NOT trap: `not (null like '%recruiter%')` is NULL, not
    TRUE, so a bare NOT would drop every GitHub row from a filter whose only
    job is to remove recruiters."""
    where = _filter_clauses(ParsedFilter(title_exclude=["recruiter"]), Params())
    assert "(s.title is null or not (lower(s.title) like $1))" in where
    strict = _filter_clauses(
        ParsedFilter(title_exclude=["recruiter"], strict_titles=True), Params()
    )
    assert "(s.title is not null and not (lower(s.title) like $1))" in strict


def test_employers_use_equality_for_canonical_names_and_trigram_for_the_rest():
    """infer_employer maps "@google"/"googlers"/"alphabet" to exactly "Google"
    before storing, so the equality is safe for the alias table's companies.
    "UCSF" is not in that table — and is a distinct employer from "University
    of California, San Francisco" in the corpus today — so it takes the
    trigram arm."""
    params = Params()
    where = _filter_clauses(ParsedFilter(employers=["Alphabet", "UCSF"]), params)
    assert "lower(s.employer) = any($1::text[])" in where
    assert "lower(s.employer) like $2" in where
    assert params.values == [["google"], "%ucsf%"]


def test_topics_admit_unknown_by_default_like_language():
    """topics is GitHub-derived and populated on ZERO of the 196,778 LinkedIn
    rows — the same collapse the language filter shipped with."""
    where = _filter_clauses(ParsedFilter(topics=["vllm"]), Params())
    assert "(s.topics && $1::text[] or coalesce(cardinality(s.topics), 0) = 0)" in where
    strict = _filter_clauses(ParsedFilter(topics=["vllm"], strict_topics=True), Params())
    assert "cardinality" not in strict


def test_markers_need_no_unknown_policy():
    """domain_markers is the one text facet populated on BOTH sources, so an
    empty array is a real negative rather than a missing column."""
    where = _filter_clauses(ParsedFilter(markers=["ml"]), Params())
    assert "s.domain_markers && $1::text[]" in where
    assert "domain_markers is null" not in where


def test_seniority_admits_unknown_by_default_and_tenure_does_not():
    """The asymmetry is deliberate and is the biggest judgement call in the
    facet layer. seniority_rank is NULL for all 7,757 GitHub rows AND for every
    LinkedIn title with no level word ("Software Engineer"), and
    heuristic_parse sets seniority_min=3 for the commonest query in the
    product — strict there would repeat the language collapse. tenure_months is
    a numeric band, so it follows include_unknown_experience: an absence of
    dates is not evidence of time in seat."""
    where = _filter_clauses(ParsedFilter(seniority_min=3, tenure_min_months=18), Params())
    assert "(s.seniority_rank >= $1 or s.seniority_rank is null)" in where
    assert "s.tenure_months >= $2" in where
    assert "s.tenure_months is null" not in where

    flipped = _filter_clauses(
        ParsedFilter(
            seniority_min=3,
            tenure_min_months=18,
            include_unknown_seniority=False,
            include_unknown_tenure=True,
        ),
        Params(),
    )
    assert "s.seniority_rank >= $1" in flipped
    assert "s.seniority_rank is null" not in flipped
    assert "(s.tenure_months >= $2 or s.tenure_months is null)" in flipped


def test_source_and_evidence_predicates():
    params = Params()
    where = _filter_clauses(
        ParsedFilter(source="linkedin", require_code_evidence=True), params
    )
    assert "s.evidence_kind = $1" in where
    # 'both' must be included or this reads as "GitHub-only", dropping every
    # cross-linked profile — the strongest evidence in the corpus.
    assert "s.evidence_kind in ('github','both')" in where
    assert params.values == ["linkedin"]


def test_manager_and_founder_are_flag_predicates():
    """mgmt_rank's NULL is a NEGATIVE, not an unknown, so there is no policy
    switch to add — but is_manager=True is therefore implicitly LinkedIn-only,
    and is_manager=False reads as "IC or unknown"."""
    assert "s.mgmt_rank is not null" in _filter_clauses(
        ParsedFilter(is_manager=True), Params()
    )
    assert "s.mgmt_rank is null" in _filter_clauses(
        ParsedFilter(is_manager=False), Params()
    )
    assert "s.is_founder" in _filter_clauses(ParsedFilter(is_founder=True), Params())
    assert "not s.is_founder" in _filter_clauses(
        ParsedFilter(is_founder=False), Params()
    )
    # Unset emits nothing: it is not the same predicate as False.
    bare = _filter_clauses(ParsedFilter(), Params())
    assert "mgmt_rank" not in bare and "is_founder" not in bare


def test_predicate_summary_lists_only_facets_that_became_sql():
    """Read by a human asking "why did this junk match" and by the eval
    harness's filter_violations@k. Facet name -> the COERCED value."""
    parsed = ParsedFilter(
        archetype_id="founding_ml",
        metros=["sf_bay_area"],
        titles=["ml engineer"],
        seniority_min=3,
        is_manager=False,
        require_code_evidence=True,
        free_text="shipped training infrastructure",
    )
    summary = parsed.to_predicate_summary()
    assert summary == {
        "archetype_id": "founding_ml",
        "metros": ["sf_bay_area"],
        "titles": ["ml engineer"],
        "seniority_min": 3,
        "is_manager": False,
        "require_code_evidence": True,
    }
    # free_text is a ranking input, not a filter; empty facets are absent
    # rather than present-and-empty.
    assert "free_text" not in summary
    assert ParsedFilter().to_predicate_summary() == {}


def test_parser_version_and_vocab_hash_are_both_in_the_cache_key():
    """Editing an alias file must invalidate cached parses: the same query
    hashes differently under a different vocabulary."""
    from foray.api import parse as parse_module

    baseline = parse_module._query_hash("senior backend engineer")
    original_hash = parse_module.vocab_hash
    original_version = parse_module.PARSER_VERSION
    try:
        parse_module.vocab_hash = lambda: "different-vocabulary"
        assert parse_module._query_hash("senior backend engineer") != baseline
    finally:
        parse_module.vocab_hash = original_hash
    try:
        parse_module.PARSER_VERSION = original_version + 1
        assert parse_module._query_hash("senior backend engineer") != baseline
    finally:
        parse_module.PARSER_VERSION = original_version
    assert parse_module._query_hash("senior backend engineer") == baseline


def test_parsed_filter_from_dict_drops_unknown_keys():
    """A version rollback replays cached dicts with keys this build lacks;
    ParsedFilter(**data) would TypeError and 500 the search. from_dict must
    degrade to defaults instead (the PARSER_VERSION bump is the primary
    protection; this is the belt-and-braces)."""
    parsed = ParsedFilter.from_dict(
        {"archetype_id": "founding_ml", "languages": ["python"], "from_the_future": 1}
    )
    assert parsed.archetype_id == "founding_ml"
    assert parsed.languages == ["python"]
    assert parsed.strict_language is False  # default, not the unknown key


def test_parsed_filter_from_dict_round_trips_to_dict():
    original = ParsedFilter(
        archetype_id="senior_backend",
        metros=["SF"],
        languages=["go"],
        experience_min=5.0,
        free_text="senior backend in go",
        strict_language=True,
    )
    assert ParsedFilter.from_dict(original.to_dict()) == original


def test_gate_does_not_disturb_placeholder_numbering():
    parsed = ParsedFilter(metros=["sf_bay_area"], languages=["go"], experience_min=5)
    gated = Params()
    where = _filter_clauses(parsed, gated, gate_sql="s.est_experience_years >= 2")
    ungated = Params()
    _filter_clauses(parsed, ungated)
    assert gated.values == ungated.values
    assert _placeholders(where) == list(range(1, len(gated.values) + 1))


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
        # Retrieval now ANDs the archetype gate into every arm. These fixture
        # rows take senior_backend's non-github branch (evidence_kind <>
        # 'github' and a backend-ish title): the github branch would demand a
        # compiled primary_language plus non-fork repos, and the query needs
        # 'haskell' to keep matching. The title also gives the linkedin
        # evidence template a second substantive phrase (title + stated
        # experience) so evidence_count clears min_evidence=2.
        await conn.execute(
            """
            insert into candidate_signals (
                candidate_id, primary_language, est_experience_years,
                activity_at, last_push_at, evidence_kind, title
            )
            select id, 'haskell', 47, now(), now(), 'linkedin', 'Backend Engineer'
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
        # min_evidence now counts substantive phrases, not rendered sentences:
        # without topics these thin fixture rows would carry one real phrase
        # plus the "no scale evidence" pad and be vetoed before paging. The
        # 'ml' marker + 6y keep them inside founding_ml's retrieval gate.
        await conn.execute(
            """
            insert into candidate_signals (
                candidate_id, primary_language, est_experience_years,
                activity_at, last_push_at, domain_markers, topics
            )
            select $1, 'python', 6, now(), now(), array['ml']::text[],
                   array['vllm','inference']::text[]
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
                activity_at, last_push_at, domain_markers, topics
            )
            select id, 'python', 6, now(), now(), array['ml']::text[],
                   array['web','servers']::text[]
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
    """SET LOCAL on a pooled connection must not survive the request.

    pgvector registers ``hnsw.ef_search`` when its library loads, not at
    connect. Touch a vector type first; skip if this build has no GUC.
    """
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(settings.database_url))
    try:
        try:
            await conn.execute("select null::vector")
            before = await conn.fetchval("show hnsw.ef_search")
        except asyncpg.UndefinedObjectError:
            pytest.skip("hnsw.ef_search is not a GUC in this pgvector build")
        await run_search(conn, "backend engineer rust", 5, use_llm=False)
        after = await conn.fetchval("show hnsw.ef_search")
        assert after == before
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
            # One non-fork repo, and NOT an empty list: admission() fails a
            # GitHub identity with zero repos as 'no_code_evidence', and an
            # ineligible row is assigned a NULL tsvector by design. This
            # fixture must stay admissible or the test stops exercising the
            # tsvector write and instead re-tests admission -- via an
            # `assert False is True` that says nothing about which rule fired.
            "repositories": {
                "nodes": [
                    {
                        "name": "vllm-bench",
                        "description": "throughput benchmarks for vllm serving",
                        "stargazerCount": 12,
                        "forkCount": 1,
                        "pushedAt": "2025-06-01T00:00:00Z",
                        "createdAt": "2024-01-01T00:00:00Z",
                        "isArchived": False,
                        "primaryLanguage": {"name": "Python"},
                        "repositoryTopics": {"nodes": [{"topic": {"name": "inference"}}]},
                        "languages": {
                            "edges": [{"node": {"name": "Python"}, "size": 10000}]
                        },
                    }
                ]
            },
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
        # Fail on the precondition rather than on its downstream effect: the
        # tsvector assignment is gated on this flag, so an admission change
        # that drops this fixture should report the admission verdict and its
        # reason, not a bare "search_tsv was null".
        assert signals["search_eligible"] is True, (
            f"fixture is inadmissible ({signals.get('ineligible_reason')}), "
            "so search_tsv is NULL by design -- fix the fixture, not the assert"
        )
        await _upsert_signals(conn, cid, node, signals)
        await conn.execute(UPDATE_SEARCH_TSV_SQL, [cid])
        # Non-null AND non-empty: the setweight concatenation yields a
        # non-NULL tsvector even with nothing to index, so `is not null` alone
        # would pass on a document the lexical arm's `@@` can never match.
        # This is HAS_LEXICAL_DOC_SQL's distinction between "was indexed" and
        # "is findable"; c.name and the repo above guarantee real lexemes.
        tsv = await conn.fetchval(
            "select search_tsv is not null and search_tsv <> ''::tsvector "
            "from candidate_signals where candidate_id = $1",
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

