from __future__ import annotations

from datetime import UTC, datetime, timedelta

from foray.evidence import build_evidence, templates_from_signals, validate


def _github(**overrides) -> dict:
    row = {
        "evidence_kind": "github",
        "primary_language": "go",
        "non_fork_repo_count": 14,
        "total_stars": 14000,
        "max_repo_stars": 14000,
        "commits_last_year": 812,
        "prs_last_year": 43,
        "employer": "Stripe",
        "employer_source": "company",
        "tenure_months": 38,
        "last_push_at": datetime.now(UTC) - timedelta(days=10),
        "topics": ["kubernetes", "terraform"],
        "top_repos": [{"name": "tikv/tikv", "stars": 14000}],
        "domain_markers": ["infra"],
        "est_experience_years": 8,
        "experience_confidence": "high",
        "title": None,
    }
    row.update(overrides)
    return row


def _linkedin(**overrides) -> dict:
    row = {
        "evidence_kind": "linkedin",
        "title": "Staff Engineer",
        "employer": "Stripe",
        "tenure_months": 38,
        "est_experience_years": 7,
        "experience_confidence": "medium",
        "domain_markers": ["ml", "infra"],
        "non_fork_repo_count": 0,
        "total_stars": 0,
        "commits_last_year": 0,
        "prs_last_year": 0,
        "top_repos": [],
        "topics": [],
        "last_push_at": None,
        "primary_language": None,
    }
    row.update(overrides)
    return row


def test_validate_drops_hedge_and_overlong() -> None:
    assert validate("impressive Kubernetes work") is False
    assert validate("Maintains apache/airflow, 2.1k stars") is True
    assert validate("one two three four five six seven eight nine ten eleven twelve") is False


def test_github_templates_are_checkable_and_short() -> None:
    phrases = templates_from_signals(_github())
    assert phrases
    assert all(validate(p) for p in phrases)
    assert any("tikv" in p for p in phrases)
    assert any("Stripe" in p for p in phrases)


def test_linkedin_never_emits_repo_star_commit_pr() -> None:
    phrases = templates_from_signals(_linkedin())
    blob = " ".join(phrases).lower()
    assert "star" not in blob
    assert "commit" not in blob
    assert " pr" not in f" {blob}"
    assert "repo" not in blob
    assert all(validate(p) for p in phrases)


def test_stored_hedge_is_dropped_at_read_time() -> None:
    ev = build_evidence(
        _github(),
        query="kubernetes platform",
        stored_highlights=["impressive Kubernetes work", "Maintains tikv/tikv, 14k stars"],
        stored_flags=[],
        score=70,
        display_floor=20,
    )
    assert "impressive Kubernetes work" not in ev.highlights
    assert ev.highlight_count >= 2
    assert all(validate(h) for h in ev.highlights)


def test_unscored_uses_signal_templates() -> None:
    ev = build_evidence(
        _github(),
        query="founding ml vlm",
        stored_highlights=["Maintains tikv/tikv, 14k stars"],
        score=None,
        display_floor=20,
    )
    assert ev.highlight_source == "signals"
    assert "unscored" in ev.flags


def test_evidence_grid_synthetic_rows_all_validate() -> None:
    failures = []
    for stars in (0, 3, 40, 120, 800, 2500):
        for commits in (0, 50, 200, 800):
            for kind in ("github", "linkedin"):
                row = _linkedin() if kind == "linkedin" else _github(
                    total_stars=stars,
                    max_repo_stars=stars,
                    commits_last_year=commits,
                    top_repos=[{"name": "repo", "stars": stars}] if stars else [],
                )
                ev = build_evidence(
                    row, query="platform kubernetes", score=40, display_floor=20
                )
                for phrase in ev.highlights:
                    if not validate(phrase):
                        failures.append((kind, stars, commits, phrase))
    assert failures == []
