from __future__ import annotations

import pytest

from foray.search_doc import (
    HAS_LEXICAL_DOC_SQL,
    UPDATE_SEARCH_TSV_FAST_SQL,
    UPDATE_SEARCH_TSV_SQL,
    build_websearch_or,
)


def test_both_tsv_updates_gate_on_search_eligible() -> None:
    """An inadmissible row gets a NULL tsvector, so the lexical arm's existing
    `search_tsv is not null` test (and HAS_LEXICAL_DOC_SQL) excludes it for
    free -- no second copy of the eligibility rule in the query path.
    enrich/derive.py's admission() owns the rule; this module only tests the
    flag. Both statements must carry the gate, or the fast backfill would
    quietly re-admit every row it touches."""
    for sql in (UPDATE_SEARCH_TSV_SQL, UPDATE_SEARCH_TSV_FAST_SQL):
        assert "case when s.search_eligible then" in sql
        assert "else null end" in sql


def test_has_lexical_doc_sql_excludes_empty_tsvectors() -> None:
    """Pinned exactly: this string is the single "has a lexical document"
    definition shared by status, the search count query, and both /health
    probes. `is not null` alone overcounts — an admitted profile with no
    indexable fields gets a non-NULL but EMPTY tsvector, which `@@` can never
    match, so it must not be counted as lexically findable either."""
    assert HAS_LEXICAL_DOC_SQL == (
        "s.search_tsv is not null and s.search_tsv <> ''::tsvector"
    )
    # Both halves are load-bearing: NULL (never indexed) and empty (indexed,
    # nothing to find) are different states and both must be excluded.
    assert "is not null" in HAS_LEXICAL_DOC_SQL
    assert "<> ''::tsvector" in HAS_LEXICAL_DOC_SQL


def test_negation_tokens_are_stripped() -> None:
    q = build_websearch_or("backend go --java -rust")
    assert q is not None
    assert "java" not in q
    assert "rust" not in q
    assert "go" in q


def test_framing_words_dropped_technical_words_survive() -> None:
    # The LA Fitness 'training' / 'Infrastructure United Inc.' incident: the
    # framing words (founding, shipped) are noise, but the technical words
    # (training, infrastructure) stay load-bearing.
    q = build_websearch_or(
        "founding ML engineer, shipped training infrastructure, 3-8 years"
    )
    assert q == "ml or training or infrastructure or 3-8"


def test_pure_framing_query_returns_none_for_vector_only() -> None:
    # None means "run the vector arm alone", never an empty result set.
    assert build_websearch_or("founding engineer building a team") is None


def test_cplusplus_is_quoted() -> None:
    q = build_websearch_or("systems engineer in c++")
    assert q is not None
    assert '"c++"' in q


@pytest.mark.parametrize(
    "raw",
    [
        "a & b | c",
        "foo!",
        "bar:",
        "(unbalanced",
        "a <-> b",
        "term:*",
        '"' * 3,
        "x" * 10_000,
        "emoji 🚀 vlm",
        "::: ***",
    ],
)
def test_adversarial_input_never_raises(raw: str) -> None:
    build_websearch_or(raw)  # must not raise; None is fine
