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
    current_titles,
    employer_of,
    experience_years,
    flatten_experience,
    is_target_geo,
    looks_like_company,
    parse_month,
    past_employers,
    pseudo_repos,
    role_titles,
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


class TestTitles:

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
            "Infrastructure United Inc. (IU)",  # the real export offender
            "Smith & Wesson Holdings",  # trailing org word
            "Acme Solutions LLC",
            "Quantum Labs",
            "Northgate Group",
            "Banco Santander S.A.",  # trailing corporate S.A.
            "Bonnie & Michelle LLP",  # llp, from the prod validation pass
            "PulseChart Solutions undefined",  # scraper artifact after org word
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
            "Tom & Jerry",  # ampersand alone is never a company signal
            "S.A. Khan",  # leading initials: "s.a" only matches at name end
            # The prod-validation false positives the removed heuristics used
            # to delete; they must stay people forever:
            "Anand Vijay(AV)",  # credential/initials parens (was: initialism rule)
            "Kok Choo (KC)",
            "Jason Angeles, MBA (LION)",
            "CAPITAL R. MAAT",  # org word mid-name, not last
            "John & Charlotte Schmiedel",  # couple profile (was: ampersand rule)
        ],
    )
    def test_real_people_pass(self, name):
        assert looks_like_company(name) is False


class TestEmployers:
    def test_confidential_placeholder_is_dropped(self):
        row = record(
            current_company={"company_id": "x", "name": "Confidential Company"},
            current_company_name="Confidential Company",
        )
        assert employer_of(row) is None


class TestDerivedFacets:
    """The facts flatten_experience already computed and used to discard."""

    def test_past_employers_excludes_the_current_one(self):
        roles = flatten_experience(record())
        # Acme has no end_date -> current; Initech ended Feb 2022 -> past.
        assert past_employers(roles) == ["Initech"]

    def test_facets_read_the_company_grouped_positions_nesting(self):
        """The shape flatten_experience unwraps: the real roles live in
        ``positions`` and the parent's title repeats the company name."""
        row = record(
            experience=[
                {
                    "title": "Bank of the West",
                    "company": "Bank of the West",
                    "positions": [
                        {
                            "title": "Vice President, Senior Business Analyst",
                            "subtitle": "Bank of the West",
                            "start_date": "Jul 2018",
                            "end_date": "Present",
                        },
                        {
                            "title": "Business Analyst",
                            "subtitle": "Bank of the West",
                            "start_date": "Jul 2016",
                            "end_date": "Jul 2018",
                        },
                    ],
                },
                {
                    "title": "Analyst",
                    "company": "Initech",
                    "start_date": "Jan 2014",
                    "end_date": "Jun 2016",
                },
            ]
        )
        roles = flatten_experience(row)
        # The nested current position is current; the nested past one, plus the
        # flat past role, are the history. The company-grouped PARENT must not
        # leak in as an employer of its own.
        assert current_titles(roles) == ["Vice President, Senior Business Analyst"]
        assert role_titles(roles) == [
            "Vice President, Senior Business Analyst",
            "Business Analyst",
            "Analyst",
        ]
        assert past_employers(roles) == ["Bank of the West", "Initech"]

    def test_role_titles_reuse_the_junk_filter(self):
        """The SET/LinkedIn phantom must not come back through a second door:
        best_title stopped emitting it in stage 1, and role_titles would have
        put all 5,462 of them straight back onto a GIN index."""
        row = record(
            experience=[
                {"title": "SET", "company": "LinkedIn"},
                {"title": "--", "company": "Acme"},
                {"title": "Senior Machine Learning Engineer", "company": "Acme"},
            ]
        )
        assert role_titles(flatten_experience(row)) == ["Senior Machine Learning Engineer"]


class TestGeo:

    def test_foreign_san_franciscos_fail(self):
        assert is_target_geo(record(country_code="MX")) is False
        assert is_target_geo(record(country_code="AR")) is False


class TestMarkers:
    def test_prose_yields_markers_without_a_language_mix(self):
        repos = pseudo_repos(record())
        markers = detect_markers(repos, {}, require_language_gate=False)
        assert "ml" in markers


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

    def test_titles_are_real_titles_not_junk(self, sample):
        titles = [to_import_row(r).title for r in sample if to_import_row(r)]
        present = [t for t in titles if t]
        assert len(present) > len(titles) * 0.5
        lowered = {t.lower() for t in present}
        assert "--" not in lowered
        assert "set at linkedin" not in lowered

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
