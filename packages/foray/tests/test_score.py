from __future__ import annotations

import hashlib
from datetime import UTC

import pytest

from foray.score.archetypes import load_archetypes
from foray.score.common import (
    HEDGE_WORDS,
    PER_CALL_USD,
    heuristic_result,
    parse_scoring_json,
)


def test_fifteen_archetypes_load_with_effective_rubric_and_hash():
    archetypes = load_archetypes()
    assert len(archetypes) == 15
    for arch in archetypes.values():
        # effective rubric includes the shared tail + JSON output contract
        assert "JSON object" in arch.rubric
        assert "hedge words" in arch.rubric
        assert arch.rubric_hash == hashlib.sha256(arch.rubric.encode()).hexdigest()


def test_rubric_hash_changes_when_tail_changes():
    archetypes = load_archetypes()
    arch = archetypes["founding_ml"]
    # Hashing the base rubric alone would differ from hashing the effective text;
    # the loader must hash the concatenation (so a tail edit invalidates scores).
    base_only = hashlib.sha256(arch.rubric.split("\n\n")[0].encode()).hexdigest()
    assert arch.rubric_hash != base_only


def test_gate_marker_extraction():
    archetypes = load_archetypes()
    assert archetypes["founding_ml"].gate_markers() == {"ml"}
    assert "infra" in archetypes["ml_platform"].gate_markers()


def test_per_call_cost_matches_spec():
    # 1650 in @ $0.10/MTok + 250 out @ $0.50/MTok (open-weight model ballpark).
    assert abs(PER_CALL_USD - 0.00029) < 1e-5


def test_parse_scoring_json_strips_fences_and_clamps():
    parsed = parse_scoring_json(
        '```json\n{"score": 130, "highlights": ["a", "b"], "rationale": "x", "flags": []}\n```'
    )
    assert parsed["score"] == 100  # clamped
    assert parsed["highlights"] == ["a", "b"]


def test_parse_scoring_json_raises_on_garbage():
    with pytest.raises(ValueError):
        parse_scoring_json("not json at all")


def _row(**kw):
    base = {
        "id": 1,
        "login": "x",
        "employer": "Stripe",
        "employer_source": "company",
        "primary_language": "python",
        "language_mix": {"python": 0.9},
        "total_stars": 2100,
        "max_repo_stars": 2100,
        "non_fork_repo_count": 4,
        "commits_last_year": 850,
        "prs_last_year": 120,
        "last_push_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        "est_experience_years": 6.0,
        "experience_confidence": "medium",
        "domain_markers": ["ml"],
        "oss_maintainer": True,
        "top_repos": [{"name": "fast-transformer", "stars": 2100, "description": "d", "language": "Python"}],
        "topics": ["pytorch"],
    }
    base.update(kw)
    return base


def test_heuristic_result_has_checkable_evidence_no_hedge_words():
    archetypes = load_archetypes()
    result = heuristic_result(_row(), archetypes["founding_ml"])
    assert 0 <= result["score"] <= 100
    assert 2 <= len(result["highlights"]) <= 4
    joined = " ".join(result["highlights"]).lower()
    assert not (set(joined.split()) & HEDGE_WORDS)  # never uses hedge words
    assert "Maintains fast-transformer, 2100 stars" in result["highlights"]


def test_heuristic_score_handles_unknown_experience():
    """est_experience_years is nullable now (NULL = no dated role, previously
    stored as 0.0 on 134,536 rows). Both float() sites used to TypeError on
    None; unknown now contributes nothing to the seniority bonus rather than
    crashing or being invented."""
    from foray.score.common import heuristic_score

    arch = load_archetypes()["founding_ml"]
    known = heuristic_score(_row(est_experience_years=0.0), arch)
    unknown = heuristic_score(_row(est_experience_years=None), arch)
    assert unknown == known  # unknown reads as 0 for the heuristic band

    # Same on the LinkedIn branch, which is where NULL actually occurs.
    li = {"evidence_kind": "linkedin", "est_experience_years": None,
          "experience_confidence": "low", "title": "Staff Engineer",
          "domain_markers": ["ml"]}
    assert 0 <= heuristic_score(li, arch) <= 100


def test_build_user_content_says_unknown_not_none():
    from foray.score.common import build_user_content

    row = {
        "evidence_kind": "linkedin",
        "language_mix": {},
        "embed_blob": "Dana Rivera",
        "login": "li:dana",
        "activity_at": None,
        "activity_kind": None,
        "tenure_months": None,
        "est_experience_years": None,
        "experience_confidence": "low",
        "title": "Staff Engineer",
        "employer": "Stripe",
        "employer_domain": None,
        "location_metro": "San Francisco",
        "domain_markers": ["ml"],
    }
    content = build_user_content(row)
    assert "experience unknown" in content
    assert "None" not in content


def test_gates_expand_the_recency_placeholder():
    """{{recency}} is expanded at load; the 15 hand-copied 18-or-24-month
    literals are gone, and validate_gates EXPLAINs the expansion at sync."""
    from foray.config import settings
    from foray.recency import recency_clause

    expected = recency_clause(
        settings.activity_floor_months, settings.snapshot_floor_months
    )
    archetypes = load_archetypes()
    assert len(archetypes) == 15
    for arch in archetypes.values():
        assert "{{recency}}" not in arch.gate
        assert expected in arch.gate, arch.id
        # One policy, not fifteen: no gate carries its own literal any more.
        assert "interval '18 months'" not in arch.gate


def test_rubric_hash_does_not_cover_the_gate():
    """Load-bearing: rubric_hash decides whether ~200k stored archetype_scores
    are invalidated. Expanding {{recency}} changes retrieval eligibility, not
    how any scored candidate was judged, so it must NOT rescore."""
    arch = load_archetypes()["founding_ml"]
    assert arch.rubric_hash == hashlib.sha256(arch.rubric.encode()).hexdigest()
    assert "activity_at" not in arch.rubric


def test_heuristic_flags_stale_and_thin():
    from datetime import datetime, timedelta

    old = datetime.now(UTC) - timedelta(days=900)
    result = heuristic_result(
        _row(last_push_at=old, non_fork_repo_count=1, total_stars=2, oss_maintainer=False, top_repos=[]),
        load_archetypes()["founding_ml"],
    )
    assert "stale_activity" in result["flags"]
    assert "thin_evidence" in result["flags"]
