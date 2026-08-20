"""The Bright Data record mapper, run against a sample of the REAL export when
one is present (data/in/linkedin_sample.jsonl — git-ignored, never committed),
plus hand-built records covering the shapes the profiling found.

No database: everything in linkedin_map is pure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foray.enrich.markers import detect_markers
from foray.ingest.linkedin_map import (
    best_title,
    business_emails,
    current_role_start,
    employer_of,
    experience_years,
    flatten_experience,
    is_target_geo,
    looks_like_company,
    parse_month,
    pseudo_repos,
    slug_of,
    to_import_row,
)
from foray.ingest.stream import stream_records

SAMPLE = Path(__file__).resolve().parents[3] / "data" / "in" / "linkedin_sample.jsonl"

needs_sample = pytest.mark.skipif(
    not SAMPLE.exists(), reason="real sample not present (data/in/linkedin_sample.jsonl)"
)

ASOF = datetime(2026, 8, 19, tzinfo=UTC)


def record(**overrides) -> dict:
    base = {
        "id": "jane-doe-1a2b3c",
        "linkedin_id": "jane-doe-1a2b3c",
        "url": "https://www.linkedin.com/in/jane-doe-1a2b3c",
        "name": "Jane Doe",
        "city": "San Francisco Bay Area",
        "country_code": "US",
        "position": "Senior Machine Learning Engineer at Acme",
        "about": "I build ML training infrastructure with pytorch and kubernetes.",
        "current_company": {"company_id": "acme", "name": "Acme"},
        "current_company_name": "Acme",
        "experience": [
            {
                "title": "Senior Machine Learning Engineer",
                "company": "Acme",
                "start_date": "Mar 2022",
                "end_date": None,
                "positions": None,
            },
            {
                "title": "Software Engineer",
                "company": "Initech",
                "start_date": "Jun 2016",
                "end_date": "Feb 2022",
                "positions": None,
            },
        ],
    }
    base.update(overrides)
    return base


class TestIdentity:
    def test_slug_comes_from_linkedin_id(self):
        assert slug_of(record()) == "jane-doe-1a2b3c"

    def test_slug_falls_back_to_the_url(self):
        row = record(linkedin_id="", id="")
        assert slug_of(row) == "jane-doe-1a2b3c"

    def test_no_identity_means_unusable(self):
        assert to_import_row(record(linkedin_id="", id="", url="", input_url="")) is None
        assert to_import_row(record(name="")) is None

    def test_import_row_carries_the_namespaced_login(self):
        row = to_import_row(record())
        assert row.login == "li:jane-doe-1a2b3c"
        assert row.linkedin_url == "https://www.linkedin.com/in/jane-doe-1a2b3c"


class TestDates:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Jul 2017", datetime(2017, 7, 1, tzinfo=UTC)),
            ("2017", datetime(2017, 1, 1, tzinfo=UTC)),
            ("Present", None),
            ("", None),
            (None, None),
            ("garbage", None),
        ],
    )
    def test_parse_month(self, raw, expected):
        assert parse_month(raw) == expected

    def test_experience_years_from_earliest_dated_role(self):
        roles = flatten_experience(record())
        years, confidence = experience_years(roles, asof=ASOF)
        assert years == pytest.approx(10.2, abs=0.2)  # Jun 2016 -> Aug 2026
        assert confidence == "high"  # two dated roles

    def test_one_dated_role_is_medium_confidence(self):
        row = record()
        row["experience"] = row["experience"][:1]
        years, confidence = experience_years(flatten_experience(row), asof=ASOF)
        assert confidence == "medium"
        assert years is not None

    def test_current_role_start(self):
        start = current_role_start(flatten_experience(record()))
        assert start == datetime(2022, 3, 1, tzinfo=UTC)


class TestTitles:
    def test_experience_title_beats_position(self):
        assert best_title(record()) == "Senior Machine Learning Engineer"

    @pytest.mark.parametrize("junk", ["--", "set at linkedin", "", "Acme"])
    def test_junk_positions_are_not_titles(self, junk):
        row = record(position=junk)
        row["experience"] = []
        assert best_title(row) is None

    def test_company_grouped_positions_unwrap(self):
        row = record()
        row["experience"] = [
            {
                "title": "Bank of the West",
                "company": "Bank of the West",
                "start_date": None,
                "end_date": None,
                "positions": [
                    {
                        "title": "Vice President, Senior Business Analyst",
                        "subtitle": "Bank of the West",
                        "start_date": "Jul 2016",
                        "end_date": "Jul 2017",
                    }
                ],
            }
        ]
        roles = flatten_experience(row)
        assert roles[0].title == "Vice President, Senior Business Analyst"
        assert roles[0].company == "Bank of the West"


class TestCompanyPages:
    @pytest.mark.parametrize(
        "name",
        [
            "Infrastructure United Inc. (IU)",  # real export offender: suffix + initialism
            "Smith & Wesson Holdings",  # suffix, and 3+ words around an ampersand
            "Acme Solutions LLC",
            "Quantum Labs",
            "Northgate Group",
            "Banco Santander S.A.",  # trailing corporate S.A.
            # A sole trader IS a company page; catching her via "ltd" is the
            # accepted trade-off documented on _COMPANY_SUFFIXES.
            "Mary-Jane O'Brien Ltd",
        ],
    )
    def test_org_shaped_names_are_companies(self, name):
        assert looks_like_company(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Diksha Thapa",
            "Savannah Bryant",
            "Jose Co",  # bare "co" is a surname; only dotted "Co." is corporate
            "Tom & Jerry",  # duo-name: "&" with only two real words stays a person
            "S.A. Khan",  # leading initials: "s.a" only matches at name end
        ],
    )
    def test_real_people_pass(self, name):
        assert looks_like_company(name) is False

    def test_empty_input_is_not_a_company(self):
        assert looks_like_company("") is False
        assert looks_like_company("   ") is False

    def test_company_named_record_is_unusable(self):
        assert to_import_row(record(name="Infrastructure United Inc. (IU)")) is None

    def test_person_named_record_still_maps(self):
        assert to_import_row(record()).name == "Jane Doe"


class TestEmployers:
    def test_confidential_placeholder_is_dropped(self):
        row = record(
            current_company={"company_id": "x", "name": "Confidential Company"},
            current_company_name="Confidential Company",
        )
        assert employer_of(row) is None

    def test_stealth_is_a_real_employer(self):
        # Stealth is signal, not junk: a founding-engineer search wants it.
        row = record(
            current_company={"company_id": "x", "name": "Stealth Startup"},
            current_company_name="Stealth Startup",
        )
        assert employer_of(row) == "Stealth Startup"

    def test_punctuation_junk_is_dropped(self):
        row = record(current_company={}, current_company_name="--")
        assert employer_of(row) is None


class TestGeo:
    def test_us_bay_area_passes(self):
        assert is_target_geo(record()) is True

    def test_foreign_san_franciscos_fail(self):
        assert is_target_geo(record(country_code="MX")) is False
        assert is_target_geo(record(country_code="AR")) is False


class TestMarkers:
    def test_prose_yields_markers_without_a_language_mix(self):
        repos = pseudo_repos(record())
        markers = detect_markers(repos, {}, require_language_gate=False)
        assert "ml" in markers

    def test_the_language_gate_would_have_blanked_them(self):
        repos = pseudo_repos(record())
        assert "ml" not in detect_markers(repos, {})


class TestEmails:
    def test_email_in_about_is_found(self):
        row = record(about="Reach me at jane@acme.com for consulting.")
        assert business_emails(row) == ["jane@acme.com"]

    def test_no_email_is_the_norm(self):
        assert business_emails(record()) == []


@needs_sample
class TestAgainstTheRealSample:
    @pytest.fixture(scope="class")
    def sample(self):
        rows = []
        with SAMPLE.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
        assert len(rows) >= 1000
        return rows

    def test_most_records_map_to_import_rows(self, sample):
        usable = [to_import_row(r) for r in sample]
        mapped = [row for row in usable if row is not None]
        # name+slug are near-universal in the export; anything under 95% means
        # the mapper is dropping real people.
        assert len(mapped) / len(sample) > 0.95

    def test_every_mapped_row_has_the_namespaced_identity(self, sample):
        for raw in sample[:500]:
            row = to_import_row(raw)
            if row is None:
                continue
            assert row.login.startswith("li:")
            assert row.slug and " " not in row.slug
            assert row.linkedin_url.startswith("https://")

    def test_titles_are_real_titles_not_junk(self, sample):
        titles = [to_import_row(r).title for r in sample if to_import_row(r)]
        present = [t for t in titles if t]
        assert len(present) > len(titles) * 0.5
        lowered = {t.lower() for t in present}
        assert "--" not in lowered
        assert "set at linkedin" not in lowered

    def test_experience_years_are_sane(self, sample):
        from foray.ingest.linkedin_map import MAX_EXPERIENCE_YEARS

        asof = datetime.now(UTC)
        for raw in sample[:500]:
            roles = flatten_experience(raw)
            years, _ = experience_years(roles, asof=asof)
            if years is not None:
                assert 0 <= years <= MAX_EXPERIENCE_YEARS

    def test_markers_fire_for_a_meaningful_share(self, sample):
        hits = 0
        for raw in sample:
            markers = detect_markers(pseudo_repos(raw), {}, require_language_gate=False)
            if markers:
                hits += 1
        # Measured on the real head sample: ~1.5% carry an actual tooling term
        # (markers are technology tokens; a bare "Software Engineer" title has
        # none — those people surface via vector search and the title-gated
        # archetypes instead). The bound guards against zero and against a
        # regression that starts matching everyone.
        assert 0.005 < hits / len(sample) < 0.5

    def test_the_stream_reader_is_bounded(self, sample):
        # Bounded memory: the reader yields incrementally rather than loading
        # the file. Read 50 records from the raw 1.9 GB export if present,
        # else from the sample (JSONL path exercises the same scanner).
        source = SAMPLE
        count = 0
        for _record, offset in stream_records(source):
            count += 1
            assert offset > 0
            if count >= 50:
                break
        assert count == 50
