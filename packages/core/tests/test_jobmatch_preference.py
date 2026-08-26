"""The compensation veto, and the ingest guard that kept postings out of it.

``score_pair`` drops a vetoed row outright, so both directions of an error here
are silent: a veto that never fires proposes a role paying half the candidate's
floor, and one that fires too eagerly deletes a role paying above it.
"""

from __future__ import annotations

from types import SimpleNamespace

from juicebox.jobmatch import parse as parse_mod
from juicebox.jobmatch.preference import preference_term
from juicebox.jobmatch.scrape import LOCATION_MAX, TITLE_MAX
from juicebox.models import CandidatePreference
from juicebox.search.parser import ParsedQuery, parse_compensation

# What a US posting stating "$110,000 - $130,000" parses to.
_UNDERPAYING = {"max_compensation": 130000.0, "compensation_currency": "USD"}


def _pref(**kwargs) -> CandidatePreference:
    return CandidatePreference(spec={"job_titles": ["backend engineer"]}, **kwargs)


class TestUnknownCurrencyIsNotACurrency:
    """``intake.extract`` writes "UNKNOWN" rather than guessing USD when a
    figure arrives with no stated currency. Reading it as a stated currency
    made it a mismatch against every "$" posting, and the veto never fired."""

    def test_the_sentinel_is_what_extraction_actually_writes(self):
        from juicebox.intake import extract

        record = extract.normalize_record({"target_base": 250000})
        assert record["comp_currency"] == "UNKNOWN"

    def test_a_figure_with_no_currency_is_still_compared(self):
        score, criteria = preference_term(
            _pref(target_base=250000.0, comp_currency="UNKNOWN"),
            ParsedQuery.from_dict(_UNDERPAYING),
        )
        assert score == 0.0
        assert [c["name"] for c in criteria] == ["comp_floor"]
        assert criteria[0]["veto"] is True

    def test_a_blank_currency_still_behaves_as_before(self):
        score, _ = preference_term(
            _pref(target_base=250000.0, comp_currency=""),
            ParsedQuery.from_dict(_UNDERPAYING),
        )
        assert score == 0.0

    def test_two_different_stated_currencies_are_still_refused(self):
        _, criteria = preference_term(
            _pref(target_base=250000.0, comp_currency="GBP"),
            ParsedQuery.from_dict(_UNDERPAYING),
        )
        # No rate to convert with, so neither the veto nor the credit applies.
        assert not any(c.get("veto") for c in criteria)
        assert not any(c["name"] in {"comp_floor", "pref_comp"} for c in criteria)

    def test_the_whole_path_from_a_job_description(self):
        low, high, currency = parse_compensation(
            "The base salary range for this role is $110,000 - $130,000."
        )
        assert (low, high, currency) == (110000.0, 130000.0, "USD")
        score, _ = preference_term(
            _pref(target_base=250000.0, comp_currency="UNKNOWN"),
            ParsedQuery.from_dict(
                {"max_compensation": high, "compensation_currency": currency}
            ),
        )
        assert score == 0.0, "a role paying half their floor must not be proposed"


class TestTheFloorIsLikeForLike:
    """The ceiling is read off phrases like "base salary range" -- a BASE band.
    Testing it against the larger of base and total vetoed a role paying above
    the candidate's own base target, and the row was then dropped silently."""

    def test_a_base_band_above_their_base_target_is_not_vetoed(self):
        score, criteria = preference_term(
            _pref(target_base=150000.0, target_total=200000.0, comp_currency="USD"),
            ParsedQuery.from_dict(
                {"max_compensation": 190000.0, "compensation_currency": "USD"}
            ),
        )
        assert score is not None and score > 0.0
        assert not any(c.get("veto") for c in criteria)
        comp = next(c for c in criteria if c["name"] == "pref_comp")
        assert comp["score"] == 1.0

    def test_a_band_below_their_base_target_is_still_vetoed(self):
        score, criteria = preference_term(
            _pref(target_base=250000.0, target_total=300000.0, comp_currency="USD"),
            ParsedQuery.from_dict(_UNDERPAYING),
        )
        assert score == 0.0
        assert criteria[0]["name"] == "comp_floor"

    def test_a_total_only_preference_still_has_a_floor(self):
        score, criteria = preference_term(
            _pref(target_total=300000.0, comp_currency="USD"),
            ParsedQuery.from_dict(_UNDERPAYING),
        )
        assert score == 0.0
        assert criteria[0]["name"] == "comp_floor"

    def test_a_posting_with_no_stated_pay_is_never_vetoed(self):
        score, criteria = preference_term(
            _pref(target_base=250000.0, comp_currency="USD"),
            ParsedQuery.from_dict({"job_titles": ["backend engineer"]}),
        )
        assert score != 0.0
        assert not any(c.get("veto") for c in criteria)


class TestIdentityRespectsTheColumnWidth:
    """``job_postings.title`` is String(240) and Postgres rejects an over-length
    value rather than truncating it, so a model echoing a long page heading
    failed jobs.parse five times and the posting never left pending_parse."""

    def _resolve(self, posting, data):
        class FakeLLM:
            available = True

            def json_fast(self, **kwargs):
                return SimpleNamespace(data=data)

        import juicebox.llm.client as client_mod

        original = client_mod.get_llm
        client_mod.get_llm = lambda: FakeLLM()
        try:
            return parse_mod.resolve_identity(posting)
        finally:
            client_mod.get_llm = original

    def test_an_over_long_identity_is_cut_to_the_column_width(self, session, account):
        from juicebox.models import JobPosting

        posting = JobPosting(
            org_id=account["org_id"],
            source="exa",
            source_id="wide-1",
            title="Senior Engineer at Acme | Careers",
            company="",
            location="",
            jd_text="Acme is hiring.",
            status="pending_parse",
        )
        rejected = self._resolve(
            posting,
            {
                "is_posting": True,
                "title": "Staff Engineer " * 40,
                "company": "The Very Long Company Name " * 20,
                "location": "Somewhere In Particular " * 20,
            },
        )
        assert rejected == ""
        assert len(posting.title) <= TITLE_MAX
        assert len(posting.company) <= TITLE_MAX
        assert len(posting.location) <= LOCATION_MAX

    def test_the_row_still_writes_on_postgres_widths(self, session, account):
        from juicebox.models import JobPosting

        posting = JobPosting(
            org_id=account["org_id"],
            source="exa",
            source_id="wide-2",
            title="Founding Engineer at Ventura | Y Combinator",
            company="",
            location="",
            jd_text="Ventura is hiring.",
            status="pending_parse",
        )
        self._resolve(
            posting,
            {
                "is_posting": True,
                "title": "Principal Platform Engineer " * 30,
                "company": "Ventura",
                "location": "New York",
            },
        )
        session.add(posting)
        session.commit()
        assert len(posting.title) == TITLE_MAX
