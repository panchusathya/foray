from __future__ import annotations

from datetime import UTC, datetime

from foray.enrich.derive import (
    admission,
    compute_language_mix,
    derive_linkedin_signals,
    derive_signals,
    merge_signals,
    normalize_repos,
)
from foray.enrich.employer import infer_employer
from foray.enrich.markers import detect_markers
from foray.ingest.activity import URN_TIMESTAMP_SHIFT
from tests.fixtures import FRONTEND_NODE, ML_ENGINEER_NODE

ASOF = datetime(2026, 8, 1, tzinfo=UTC)


def _urn(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000) << URN_TIMESTAMP_SHIFT)


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


def test_strong_ml_term_bypasses_python_gate():
    repos = [
        {
            "name": "megatron-kernels",
            "description": "vllm runtime and megatron training in CUDA",
            "topics": ["cuda"],
        }
    ]
    assert "ml" in detect_markers(repos, {"cpp": 0.85, "cuda": 0.15})


def test_bio_vlm_awards_ml_without_python_majority():
    repos = [{"name": "train-loop", "description": "data prep utilities", "topics": []}]
    assert "ml" in detect_markers(
        repos, {"cpp": 1.0}, bio="I trained VLMs from scratch with a custom stack"
    )
    assert "ml" not in detect_markers(repos, {"cpp": 1.0}, bio="I like cooking")


def test_weak_ml_term_still_needs_python_gate():
    repos = [{"name": "notes", "description": "playing with transformers and clip", "topics": []}]
    assert "ml" not in detect_markers(repos, {"cpp": 1.0})


def test_derive_includes_bio_in_markers():
    s = derive_signals(
        {
            "name": "cuda",
            "login": "cuda",
            "createdAt": "2018-01-01T00:00:00Z",
            "bio": "I trained VLMs from scratch",
            "repositories": {
                "nodes": [
                    {
                        "name": "kernels",
                        "description": "cuda kernels",
                        "stargazerCount": 10,
                        "forkCount": 0,
                        "pushedAt": "2025-01-01T00:00:00Z",
                        "createdAt": "2020-01-01T00:00:00Z",
                        "isArchived": False,
                        "primaryLanguage": {"name": "C++"},
                        "repositoryTopics": {"nodes": []},
                        "languages": {"edges": [{"size": 10000, "node": {"name": "C++"}}]},
                    }
                ]
            },
            "contributionsCollection": {
                "totalCommitContributions": 10,
                "totalPullRequestContributions": 1,
                "restrictedContributionsCount": 0,
            },
        }
    )
    assert "ml" in s["domain_markers"]


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


# ------------------------------------------------- the LinkedIn activity ladder --


def _li_record(**kw) -> dict:
    base = {
        "name": "Dana Rivera",
        "id": "dana-rivera",
        "city": "San Francisco",
        "about": "x" * 200,
        "current_company": {"name": "Stripe"},
        "experience": [
            {"title": "Staff Engineer", "company": "Stripe", "start_date": "Jan 2020"},
            {
                "title": "Software Engineer",
                "company": "Acme",
                "start_date": "Mar 2016",
                "end_date": "Dec 2019",
            },
        ],
    }
    base.update(kw)
    return base


def test_activity_at_is_a_decoded_feed_date_not_the_import_clock():
    """The whole point of stage 2: activity_at was `asof` (raw_profiles
    .fetched_at = the import wall-clock), which let 196,778/196,778 LinkedIn
    rows pass the 24-month floor stamped 'snapshot'."""
    post_dt = datetime(2025, 5, 17, tzinfo=UTC)
    signals = derive_linkedin_signals(
        _li_record(activity=[{"id": _urn(post_dt)}, {"id": "junk"}]), asof=ASOF
    )
    assert signals["activity_kind"] == "linkedin_activity"
    assert signals["activity_at"].date() == post_dt.date()
    assert signals["activity_at"] != ASOF
    # The snapshot claim survives, but only where it belongs.
    assert signals["profile_asof"] == ASOF


def test_activity_at_falls_back_to_posts_then_to_null():
    posts = [
        {"created_at": "Wed Apr 05 2023 13:45:09 GMT+0000 (Coordinated Universal Time)"}
    ]
    signals = derive_linkedin_signals(_li_record(posts=posts), asof=ASOF)
    assert signals["activity_kind"] == "linkedin_post"
    assert signals["activity_at"].year == 2023

    bare = derive_linkedin_signals(_li_record(), asof=ASOF)
    assert bare["activity_at"] is None
    assert bare["activity_kind"] is None
    assert bare["profile_asof"] == ASOF  # recency now rests on the snapshot arm


def test_est_experience_years_is_none_when_unknown():
    """134,536 prod rows stored 0 meaning UNKNOWN, and every consumer read it
    as 'zero years'."""
    undated = derive_linkedin_signals(
        _li_record(experience=[{"title": "Engineer", "company": "Acme"}]), asof=ASOF
    )
    assert undated["est_experience_years"] is None

    dated = derive_linkedin_signals(_li_record(), asof=ASOF)
    assert dated["est_experience_years"] is not None
    assert dated["est_experience_years"] > 9  # earliest dated role: Mar 2016


def test_github_experience_keeps_the_account_age_proxy():
    """The asymmetry is deliberate: every GitHub node has a createdAt, so this
    path is never NULL; LinkedIn has no proxy of comparable strength."""
    s = derive_signals(ML_ENGINEER_NODE)
    assert s["est_experience_years"] is not None
    assert s["est_experience_years"] > 0


def test_merge_prefers_the_freshest_dated_claim_and_keeps_none_when_both_none():
    push = datetime(2024, 2, 2, tzinfo=UTC)
    github = derive_signals(ML_ENGINEER_NODE)
    github["activity_at"], github["activity_kind"] = push, "push"

    # A LinkedIn snapshot can no longer mask a stale GitHub push: with no
    # decodable feed activity the LinkedIn side contributes nothing.
    li_bare = derive_linkedin_signals(_li_record(), asof=ASOF)
    merged = merge_signals(github, li_bare)
    assert merged["activity_at"] == push
    assert merged["activity_kind"] == "push"

    # Real LinkedIn activity that IS fresher wins.
    fresh = datetime(2026, 1, 9, tzinfo=UTC)
    li_active = derive_linkedin_signals(
        _li_record(activity=[{"id": _urn(fresh)}]), asof=ASOF
    )
    merged2 = merge_signals(github, li_active)
    assert merged2["activity_kind"] == "linkedin_activity"
    assert merged2["activity_at"].date() == fresh.date()

    # Both unknown stays unknown.
    github["activity_at"], github["activity_kind"] = None, None
    merged3 = merge_signals(github, li_bare)
    assert merged3["activity_at"] is None
    assert merged3["activity_kind"] is None


# ---------------------------------------------------------- admission control --


def test_admission_admits_a_corroborated_title():
    signals = derive_linkedin_signals(_li_record(), asof=ASOF)
    assert signals["search_eligible"] is True
    assert signals["ineligible_reason"] is None


def test_admission_rejects_the_contentless_linkedin_row():
    """The 24,698-row prod population: no title, no about-text, no experience
    entries -- yet each holds a signals row, an embedding and a tsvector."""
    empty = derive_linkedin_signals(
        {"name": "Nobody", "id": "nobody", "experience": []}, asof=ASOF
    )
    assert empty["search_eligible"] is False
    assert empty["ineligible_reason"] == "no_title"


def test_admission_rejects_a_bare_title():
    bare = derive_linkedin_signals(
        {
            "name": "Solo",
            "id": "solo",
            "position": "Coordinator",
            "experience": [],
            "about": "short",
        },
        asof=ASOF,
    )
    assert bare["title"] == "Coordinator"
    assert bare["search_eligible"] is False
    assert bare["ineligible_reason"] == "bare_title"


def test_admission_corroborating_facts_are_each_sufficient():
    base = {"evidence_kind": "linkedin", "title": "Engineer", "domain_markers": []}
    assert admission(base, about_chars=0, role_count=1) == (False, "bare_title")
    assert admission(base, about_chars=80, role_count=1)[0] is True
    assert admission(base, about_chars=0, role_count=2)[0] is True
    assert admission({**base, "employer": "Stripe"}, about_chars=0, role_count=1)[0]
    assert admission(
        {**base, "current_role_start_at": ASOF}, about_chars=0, role_count=1
    )[0]
    assert admission({**base, "domain_markers": ["ml"]}, about_chars=0, role_count=1)[0]


def test_admission_github_needs_a_non_fork_repo_not_a_title():
    """title is NULL on all 7,757 prod GitHub rows, so requiring it here would
    delete the whole GitHub population."""
    ok = admission(
        {"evidence_kind": "github", "title": None, "non_fork_repo_count": 1},
        about_chars=0,
        role_count=0,
    )
    assert ok == (True, None)
    empty = admission(
        {"evidence_kind": "github", "title": None, "non_fork_repo_count": 0},
        about_chars=0,
        role_count=0,
    )
    assert empty == (False, "no_code_evidence")


def test_admission_reasons_partition_the_ineligible_population():
    reasons = {
        admission(
            {"evidence_kind": k, "title": t, "non_fork_repo_count": n, "domain_markers": []},
            about_chars=0,
            role_count=1,
        )[1]
        for k, t, n in (
            ("linkedin", None, 0),
            ("linkedin", "Coordinator", 0),
            ("github", None, 0),
            ("both", None, 0),
        )
    }
    assert reasons == {"no_title", "bare_title", "no_code_evidence"}


def test_merged_row_recomputes_admission_on_the_merged_facts():
    github = derive_signals(ML_ENGINEER_NODE)
    li = derive_linkedin_signals(_li_record(), asof=ASOF)
    merged = merge_signals(github, li)
    assert merged["evidence_kind"] == "both"
    assert merged["search_eligible"] is True
    assert merged["ineligible_reason"] is None


# ------------------------------------------------------- derived facets (013) --

FACET_KEYS = (
    "seniority_rank",
    "mgmt_rank",
    "is_founder",
    "seniority_source",
    "past_employers",
    "role_titles",
    "current_titles",
    "schools",
    "degrees",
    "fields_of_study",
    "country_code",
)


def test_both_sources_emit_every_facet_key():
    """The _UPSERT indexes signals[...] positionally for all 11 columns, and a
    missing key on ONE source is a KeyError on a batch of 500 that the derive
    loop swallows into enrich_errors -- i.e. a silent per-source data hole."""
    github = derive_signals(ML_ENGINEER_NODE)
    li = derive_linkedin_signals(_li_record(), asof=ASOF)
    merged = merge_signals(github, li)
    for signals in (github, li, merged):
        for key in FACET_KEYS:
            assert key in signals, key


def test_linkedin_seniority_comes_from_the_title():
    s = derive_linkedin_signals(_li_record(), asof=ASOF)
    assert s["title"] == "Staff Engineer"
    assert s["seniority_rank"] == 5
    assert s["mgmt_rank"] is None
    assert s["is_founder"] is False
    assert s["seniority_source"] == "title"


def test_a_title_with_no_level_word_falls_through_to_the_proxy():
    """The fallback is what stops seniority from being NULL for everyone whose
    title is a bare "Software Engineer" -- but it must announce itself."""
    s = derive_linkedin_signals(
        _li_record(
            experience=[
                {"title": "Software Engineer", "company": "Stripe", "start_date": "Jan 2020"},
                {
                    "title": "Software Engineer",
                    "company": "Acme",
                    "start_date": "Mar 2016",
                    "end_date": "Dec 2019",
                },
            ]
        ),
        asof=ASOF,
    )
    assert s["title"] == "Software Engineer"
    assert s["seniority_source"] == "experience_proxy"
    # Mar 2016 -> Aug 2026 is >8 years, the top proxy band.
    assert s["seniority_rank"] == 5


def test_unknown_title_and_unknown_experience_means_unknown_seniority():
    s = derive_linkedin_signals(
        _li_record(experience=[{"title": "Software Engineer", "company": "Acme"}]),
        asof=ASOF,
    )
    assert s["est_experience_years"] is None
    assert s["seniority_rank"] is None
    assert s["seniority_source"] is None


def test_a_management_title_records_mgmt_and_keeps_the_title_provenance():
    """"Engineering Manager" has no IC level word, so seniority_rank stays
    NULL -- and we must NOT patch it in from the proxy, because a row whose two
    ordinals came from different evidence classes cannot be described by one
    seniority_source."""
    s = derive_linkedin_signals(
        _li_record(
            experience=[
                {"title": "Engineering Manager", "company": "Stripe", "start_date": "Jan 2020"}
            ]
        ),
        asof=ASOF,
    )
    assert s["seniority_rank"] is None
    assert s["mgmt_rank"] == 1
    assert s["seniority_source"] == "title"


def test_founder_titles_set_the_flag():
    s = derive_linkedin_signals(
        _li_record(
            experience=[
                {"title": "Founding Engineer", "company": "Stealth", "start_date": "Jan 2024"}
            ]
        ),
        asof=ASOF,
    )
    assert s["is_founder"] is True
    assert s["seniority_source"] == "title"


def test_linkedin_career_and_education_facets_are_populated():
    s = derive_linkedin_signals(
        _li_record(
            country_code="US",
            education=[
                {"title": "Stanford University", "degree": "MS", "field": "Computer Science"},
                {"title": None, "degree": None, "field": None},
            ],
        ),
        asof=ASOF,
    )
    # Acme is the PAST employer (it has an end_date); Stripe is current and
    # lives in `employer`, not here.
    assert s["past_employers"] == ["Acme"]
    assert s["role_titles"] == ["Staff Engineer", "Software Engineer"]
    assert s["current_titles"] == ["Staff Engineer"]
    assert s["schools"] == ["Stanford University"]
    assert s["degrees"] == ["MS"]
    assert s["fields_of_study"] == ["Computer Science"]
    assert s["country_code"] == "US"


def test_github_seniority_is_always_a_proxy_and_never_null():
    """title is NULL on all 7,757 prod GitHub rows, so a title-only seniority
    column would make `seniority_rank >= 4` mean "and is a LinkedIn row".
    est_experience_years is a max() of two floats defaulting to 0.0 on this
    path, so the proxy always has an input."""
    s = derive_signals(ML_ENGINEER_NODE)
    assert s["title"] is None
    assert s["est_experience_years"] is not None
    assert s["seniority_rank"] is not None
    assert s["seniority_source"] == "experience_proxy"
    # No title means no management or founder claim is available at all.
    assert s["mgmt_rank"] is None
    assert s["is_founder"] is False


def test_github_linkedin_only_facets_are_empty_arrays_not_none():
    """Empty arrays keep every array predicate (&&, @>) total; NULL would need
    an IS NULL arm in each one, which is where unknown-policy bugs live."""
    s = derive_signals(ML_ENGINEER_NODE)
    for key in ("past_employers", "role_titles", "current_titles", "schools",
                "degrees", "fields_of_study"):
        assert s[key] == [], key
    # GitHub's profile location is free text with no country field, and
    # guessing one would be worse than unknown.
    assert s["country_code"] is None


def test_location_metro_is_canonical_for_both_sources():
    """It used to be raw city text for LinkedIn and hardcoded NULL for GitHub,
    which made any metro predicate on the column "and is a LinkedIn row"."""
    github = derive_signals(ML_ENGINEER_NODE)
    li = derive_linkedin_signals(_li_record(city="San Francisco"), asof=ASOF)
    assert github["location_metro"] is not None
    assert li["location_metro"] is not None
    # Whatever the canonicaliser does, it must be case/whitespace-stable --
    # that is the minimum a key on an equality-filtered column has to be.
    spaced = derive_linkedin_signals(_li_record(city="  SAN FRANCISCO  "), asof=ASOF)
    assert spaced["location_metro"] == li["location_metro"]


def test_merge_never_downgrades_a_title_reading_to_the_proxy():
    """THE ordering assertion. The GitHub side ALWAYS carries a proxy rank
    (account age is never NULL), so a coalesce or a max() would let an
    account-age band overwrite a real title on exactly the rows we know most
    about."""
    github = derive_signals(ML_ENGINEER_NODE)
    assert github["seniority_source"] == "experience_proxy"
    li = derive_linkedin_signals(
        _li_record(
            experience=[
                {"title": "Engineering Manager", "company": "Stripe", "start_date": "Jan 2020"}
            ]
        ),
        asof=ASOF,
    )
    assert li["seniority_source"] == "title"

    merged = merge_signals(github, li)
    assert merged["seniority_source"] == "title"
    assert merged["mgmt_rank"] == 1
    # ...and the LinkedIn NULL survives too: the proxy does not sneak in to
    # fill it, because then one source column would describe two provenances.
    assert merged["seniority_rank"] is None


def test_merge_recomputes_the_proxy_when_neither_side_read_a_title():
    github = derive_signals(ML_ENGINEER_NODE)
    li = derive_linkedin_signals(
        _li_record(
            experience=[
                {"title": "Software Engineer", "company": "Stripe", "start_date": "Jan 2024"}
            ]
        ),
        asof=ASOF,
    )
    assert li["seniority_source"] == "experience_proxy"
    merged = merge_signals(github, li)
    assert merged["seniority_source"] == "experience_proxy"
    # Computed from the MERGED experience number (LinkedIn's dated role wins
    # over GitHub's account age), not copied from either side.
    from foray.seniority import seniority_from_experience

    assert merged["seniority_rank"] == seniority_from_experience(
        merged["est_experience_years"]
    )


def test_merge_gives_linkedin_the_career_facets_outright():
    github = derive_signals(ML_ENGINEER_NODE)
    li = derive_linkedin_signals(
        _li_record(education=[{"title": "MIT", "degree": None, "field": None}]),
        asof=ASOF,
    )
    merged = merge_signals(github, li)
    assert merged["role_titles"] == li["role_titles"]
    assert merged["past_employers"] == li["past_employers"]
    assert merged["schools"] == ["MIT"]
    assert merged["country_code"] == li["country_code"]
    # ...while the code facts stay GitHub's.
    assert merged["primary_language"] == github["primary_language"]
    assert merged["total_stars"] == github["total_stars"]
