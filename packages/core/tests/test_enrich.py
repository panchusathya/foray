from datetime import date

import pytest

from juicebox.enrich import taxonomy as tx
from juicebox.enrich.email import detect_pattern, guess_emails, learn_org_pattern
from juicebox.enrich.normalize import (
    compute_years_experience,
    normalize_candidate,
    parse_year_month,
)


class TestTaxonomy:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Senior ML Engineer", "machine learning engineer"),
            ("Sr. Software Developer", "software engineer"),
            ("Staff Backend Engineer", "backend engineer"),
            ("VP of Engineering", "engineering"),
            ("SDR", "sales development representative"),
        ],
    )
    def test_normalize_title(self, raw, expected):
        assert tx.normalize_title(raw) == expected

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Senior Software Engineer", "senior"),
            ("Staff Engineer", "staff"),
            ("VP of Product", "vp"),
            ("Chief Technology Officer", "cxo"),
            ("Junior Analyst", "junior"),
            ("Software Engineer", "mid"),
        ],
    )
    def test_infer_seniority(self, title, expected):
        assert tx.infer_seniority_from_title(title) == expected

    def test_seniority_distance_is_signed(self):
        assert tx.seniority_distance("staff", "senior") == 1
        assert tx.seniority_distance("junior", "senior") == -2
        assert tx.seniority_distance("unknown", "senior") is None

    def test_skill_aliases_collapse(self):
        assert tx.normalize_skill("K8s") == "kubernetes"
        assert tx.normalize_skill("NodeJS") == "node.js"
        assert tx.normalize_skill("Large Language Models") == "llm"

    def test_extract_skills_from_prose(self):
        found = tx.extract_skills_from_text(
            "Built RAG pipelines in Python on top of Kubernetes and Postgres"
        )
        assert {"python", "kubernetes", "postgresql"} <= set(found)

    def test_skill_match_strength_tiers(self):
        skills = {"tensorflow"}
        assert tx.skill_match_strength("tensorflow", skills, "") == 1.0
        assert tx.skill_match_strength("pytorch", skills, "") == 0.7
        assert tx.skill_match_strength("rust", skills, "shipped rust services") == 0.45
        assert tx.skill_match_strength("cobol", skills, "") == 0.0

    def test_location_metro_matching(self):
        assert tx.location_match(["Oakland", "CA"], "san francisco") == 0.75
        assert tx.location_match(["San Francisco"], "sf") == 1.0
        assert tx.location_match(["Berlin"], "new york") == 0.0


class TestNormalizeDates:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2021-04", (2021, 4)),
            ("2021", (2021, 1)),
            ("2021/7", (2021, 7)),
            ("Apr 2021", (2021, 4)),
            ("", None),
            ("not a date", None),
        ],
    )
    def test_parse_year_month(self, raw, expected):
        assert parse_year_month(raw) == expected

    def test_overlapping_roles_are_not_double_counted(self):
        payload = {
            "full_name": "Overlap Person",
            "experiences": [
                {"company": "A", "title": "Engineer", "start_date": "2018-01", "end_date": "2022-01"},
                {"company": "B", "title": "Advisor", "start_date": "2019-01", "end_date": "2021-01"},
            ],
        }
        candidate = normalize_candidate(payload, today=date(2024, 1, 1))
        assert candidate.years_experience == 4.0
        assert compute_years_experience(candidate.experiences, today=date(2024, 1, 1)) == 4.0


class TestNormalizeCandidate:
    def test_derives_current_role_and_seniority(self):
        candidate = normalize_candidate(
            {
                "full_name": "Rae Silva",
                "location": "Austin, TX, United States",
                "skills": ["Golang", "K8s"],
                "experiences": [
                    {
                        "company": "Ramp Inc.",
                        "title": "Staff Backend Engineer",
                        "start_date": "2021-02",
                        "is_current": True,
                    },
                    {
                        "company": "Plaid",
                        "title": "Backend Engineer",
                        "start_date": "2018-01",
                        "end_date": "2021-01",
                    },
                ],
            },
            today=date(2024, 2, 1),
        )
        assert candidate.current_title == "Staff Backend Engineer"
        assert candidate.current_company == "Ramp Inc."
        assert candidate.seniority == "staff"
        assert candidate.location_city == "Austin"
        assert {"go", "kubernetes"} <= set(candidate.skills)
        assert "Staff Backend Engineer" in candidate.doc_text

    def test_invalid_emails_dropped(self):
        candidate = normalize_candidate(
            {"full_name": "X Y", "emails": ["good@example.com", "not-an-email", ""]}
        )
        assert candidate.emails == ["good@example.com"]

    def test_source_id_falls_back_to_a_stable_value(self):
        candidate = normalize_candidate({"full_name": "Jo Kim", "emails": ["jo@k.com"]})
        assert candidate.source_id == "jo@k.com"


class TestEmailPatterns:
    def test_detect_and_learn_pattern(self):
        assert detect_pattern("aokonkwo@acme.com", "Ada", "Okonkwo") == "flast"
        pattern = learn_org_pattern(
            [
                ("aokonkwo@acme.com", "Ada", "Okonkwo"),
                ("bsilva@acme.com", "Bo", "Silva"),
                ("cara.diallo@acme.com", "Cara", "Diallo"),
            ]
        )
        assert pattern == "flast"

    def test_known_pattern_wins(self):
        guesses = guess_emails("Ada", "Okonkwo", "acme.com", known_pattern="flast")
        assert guesses[0].email == "aokonkwo@acme.com"
        assert guesses[0].confidence > 0.9
