from __future__ import annotations

from foray.enrich.derive import compute_language_mix, derive_signals, normalize_repos
from foray.enrich.employer import infer_employer
from foray.enrich.markers import detect_markers
from tests.fixtures import FRONTEND_NODE, ML_ENGINEER_NODE


def test_language_mix_normalizes_to_fractions():
    repos = normalize_repos(ML_ENGINEER_NODE)
    mix = compute_language_mix(repos)
    assert abs(sum(mix.values()) - 1.0) < 1e-6
    assert mix["python"] > mix["cuda"]


def test_derive_signals_ml_engineer():
    s = derive_signals(ML_ENGINEER_NODE)
    assert s["primary_language"] == "python"
    assert s["total_stars"] == 2103
    assert s["max_repo_stars"] == 2100
    assert s["non_fork_repo_count"] == 2
    assert 0.4 < s["archived_fraction"] < 0.6  # 1 of 2 archived
    assert s["oss_maintainer"] is True  # 2100 >= 100
    assert s["commits_last_year"] == 850
    assert s["prs_last_year"] == 120
    assert s["experience_confidence"] in {"high", "medium"}
    assert s["est_experience_years"] >= 6
    # ml + gpu markers should fire (python fraction > 0.2, cuda term present)
    assert "ml" in s["domain_markers"]
    assert "gpu" in s["domain_markers"]
    # top repos sorted by stars, capped at 5
    assert s["top_repos"][0]["name"] == "fast-transformer"


def test_marker_word_boundary_no_false_positive():
    # "go" is a language/marker-ish token; it must not match inside "google".
    repos = [{"name": "google-clone", "description": "a googly thing", "topics": []}]
    markers = detect_markers(repos, {"go": 0.9})
    assert "infra" not in markers  # nothing infra here
    # A real infra term does fire:
    repos2 = [{"name": "k8s-operator", "description": "kubernetes controller", "topics": []}]
    assert "infra" in detect_markers(repos2, {})


def test_frontend_marker_requires_js_fraction():
    repos = normalize_repos(FRONTEND_NODE)
    mix = compute_language_mix(repos)
    assert "frontend" in detect_markers(repos, mix)  # TS fraction > 0.2

    # Same repo text but no JS/TS in the mix -> frontend must NOT fire.
    assert "frontend" not in detect_markers(repos, {"python": 1.0})


def test_employer_priority_company_first():
    employer, source = infer_employer(ML_ENGINEER_NODE)
    assert employer == "Stripe"
    assert source == "company"


def test_employer_falls_through_to_org_then_email():
    node = {"company": "", "organizations": {"nodes": [{"login": "acme", "name": "Acme Inc"}]}}
    assert infer_employer(node) == ("Acme Inc", "org")

    node2 = {"company": "", "organizations": {"nodes": []}, "email": "person@acmecorp.com"}
    employer, source = infer_employer(node2)
    assert source == "email_domain"
    assert "acmecorp" in employer

    node3 = {"company": "", "organizations": {"nodes": []}, "email": "person@gmail.com"}
    assert infer_employer(node3) == (None, None)  # freemail is not an employer


def test_community_org_is_not_employer():
    node = {"company": "", "organizations": {"nodes": [{"login": "freecodecamp", "name": "freeCodeCamp"}]}}
    assert infer_employer(node) == (None, None)
