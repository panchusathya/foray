"""Search-quality regressions: the queries that returned the wrong people.

Every case here was a live wrong answer, not a hypothetical. The parser cases
assert on the parsed values because that is where each one goes wrong; the
Postgres cases assert on the SQL and its bound parameters because SQLite runs
the in-process backend and never sees either bug.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from juicebox.enrich import taxonomy as tx
from juicebox.search.filters import apply_filters
from juicebox.search.index import CandidateDoc
from juicebox.search.parser import ParsedQuery, heuristic_parse
from juicebox.search.postgres import PostgresIndex, _as_tsquery
from juicebox.search.scoring import _score_education, _score_location


def _doc(**overrides) -> CandidateDoc:
    base = dict(
        id="c1",
        full_name="Ada Okonkwo",
        headline="",
        summary="",
        current_title="Backend Engineer",
        current_title_normalized="backend engineer",
        current_company="Acme Inc.",
        current_company_normalized=tx.normalize_company("Acme Inc."),
        seniority="senior",
        years_experience=6.0,
        skills=set(),
        industries=set(),
        location_parts=["New York", "NY", "United States"],
        location_city="New York",
        location_region="NY",
        location_country="United States",
        open_to_remote=False,
        emails=[],
        linkedin_url="",
        doc_text="",
        haystack="backend engineer at acme inc",
    )
    base.update(overrides)
    return CandidateDoc(**base)


class _Row(SimpleNamespace):
    pass


class _RecordingSession:
    """Captures the SQL ``_eligible`` builds without needing a live Postgres."""

    def __init__(self) -> None:
        self.sql = ""
        self.params: dict = {}

    def execute(self, statement, params=None):
        self.sql = str(statement)
        self.params = params or {}
        return SimpleNamespace(
            one=lambda: _Row(
                eligible=[],
                excluded_company=0,
                excluded_title=0,
                negative_keyword=0,
                below_min_years=0,
                above_max_years=0,
            )
        )


# --------------------------------------------------------------------------
# a multi-word negative keyword is a phrase, not a token
# --------------------------------------------------------------------------


class TestTsquery:
    """``to_tsquery`` needs an operator between lexemes. A bare space is a
    syntax error, so "engineers not in the US" 500'd the whole request."""

    def test_a_multi_word_term_becomes_a_phrase_match(self):
        assert _as_tsquery(["in the us"]) == "(in <-> the <-> us)"

    def test_a_single_token_is_unchanged(self):
        assert _as_tsquery(["crypto"]) == "crypto"

    def test_terms_are_still_ored_together(self):
        assert _as_tsquery(["crypto", "in the us"]) == "crypto | (in <-> the <-> us)"

    def test_no_term_ever_reaches_postgres_with_a_bare_space(self):
        query = _as_tsquery(["in the us", "not looking for agencies", "go"])
        assert query is not None
        for chunk in query.replace("(", " ").replace(")", " ").split("|"):
            words = [w for w in chunk.split() if w != "<->"]
            assert len(words) == chunk.count("<->") + 1

    def test_tsquery_operators_in_a_term_are_stripped(self):
        assert _as_tsquery(["fake & (evil)"]) == "(fake <-> evil)"

    def test_punctuated_skills_are_left_alone(self):
        assert _as_tsquery(["c++", "node.js"]) == "c++ | node.js"

    def test_nothing_usable_yields_no_query(self):
        assert _as_tsquery([]) is None
        assert _as_tsquery(["  "]) is None

    def test_the_negative_from_a_real_query_is_a_valid_tsquery(self):
        parsed = heuristic_parse("engineers not in the US")
        assert parsed.negative_keywords == ["in the us"]
        assert _as_tsquery(parsed.negative_keywords) == "(in <-> the <-> us)"


# --------------------------------------------------------------------------
# a ceiling is a ceiling
# --------------------------------------------------------------------------


class TestYearsCeiling:
    """``_YEARS_MIN_RE``'s prefix is optional, so every phrasing the ceiling
    pattern did not cover was read as a FLOOR -- the exact inverse of the
    request, with everyone junior hard-rejected as ``below_min_years``."""

    def test_no_more_than(self):
        parsed = heuristic_parse("no more than 5 years")
        assert (parsed.min_years_experience, parsed.max_years_experience) == (None, 5.0)

    def test_fewer_than(self):
        parsed = heuristic_parse("fewer than 5 years of experience")
        assert (parsed.min_years_experience, parsed.max_years_experience) == (None, 5.0)

    def test_up_to(self):
        parsed = heuristic_parse("up to 5 years of experience")
        assert (parsed.min_years_experience, parsed.max_years_experience) == (None, 5.0)

    def test_the_phrasings_that_already_worked_still_do(self):
        assert heuristic_parse("under 4 years of experience").max_years_experience == 4
        assert heuristic_parse("engineers with 3-6 years").min_years_experience == 3
        assert heuristic_parse("engineers with 3-6 years").max_years_experience == 6

    def test_a_floor_is_still_a_floor(self):
        parsed = heuristic_parse("at least 5 years of experience")
        assert (parsed.min_years_experience, parsed.max_years_experience) == (5.0, None)
        assert heuristic_parse("engineers with 8+ years").min_years_experience == 8

    def test_both_ends_stated_separately(self):
        parsed = heuristic_parse("at least 3 years and no more than 8 years")
        assert (parsed.min_years_experience, parsed.max_years_experience) == (3.0, 8.0)


# --------------------------------------------------------------------------
# a negation capture must not become a stopword exclusion
# --------------------------------------------------------------------------


class TestNegationCapture:
    """A negative keyword is an OUTRIGHT rejection. Keeping the first word of a
    long capture made "more" one of them, which deleted most of the corpus and
    booked it as a deliberate exclusion."""

    def test_a_quantity_bound_is_not_an_exclusion(self):
        parsed = heuristic_parse("senior backend engineer with no more than 5 years")
        assert parsed.negative_keywords == []
        assert "more" not in parsed.keywords and "than" not in parsed.keywords
        assert (parsed.min_years_experience, parsed.max_years_experience) == (None, 5.0)

    def test_a_real_exclusion_survives_whole(self):
        assert heuristic_parse("engineers not in the US").negative_keywords == ["in the us"]
        assert heuristic_parse("engineers without kubernetes").negative_keywords == [
            "kubernetes"
        ]

    def test_a_lone_stopword_never_becomes_a_filter(self):
        for query in ("engineers with no the", "engineers not for"):
            assert heuristic_parse(query).negative_keywords == []

    def test_the_corpus_survives_the_query_that_emptied_it(self):
        docs = [
            _doc(id="a", haystack="backend engineer who wants more ownership"),
            _doc(id="b", years_experience=25.0, haystack="staff engineer"),
        ]
        parsed = heuristic_parse("senior backend engineer with no more than 5 years")
        outcome = apply_filters(docs, parsed)
        assert "negative_keyword" not in outcome.excluded
        # And the ceiling now removes the veteran rather than the juniors.
        assert outcome.mask.tolist() == [True, False]
        assert outcome.excluded.get("above_max_years") == 1


# --------------------------------------------------------------------------
# degrees
# --------------------------------------------------------------------------


class TestDegreeVocabulary:
    """The query side canonicalises to ``ms``; LinkedIn stores "master of
    science". A substring test between those is a miss, so asking for a
    master's demoted every person who holds one."""

    def test_asking_for_a_masters_matches_a_master_of_science(self):
        parsed = heuristic_parse("backend engineers with a masters degree")
        assert parsed.degrees == ["ms"]
        criterion = _score_education(_doc(degrees=["master of science"]), parsed)
        assert criterion is not None
        assert criterion.score == 1.0
        assert criterion.status == "strong"

    def test_an_llm_spelling_lands_on_the_same_token(self):
        assert ParsedQuery(degrees=["Master's Degree"]).normalized().degrees == ["ms"]
        assert ParsedQuery(degrees=["Bachelor of Science"]).normalized().degrees == ["bs"]

    def test_a_bachelors_holder_is_still_a_miss_for_a_masters_query(self):
        parsed = heuristic_parse("backend engineers with a masters degree")
        criterion = _score_education(_doc(degrees=["bachelor of arts"]), parsed)
        assert criterion is not None and criterion.score == 0.0

    def test_phd_and_mba_keep_working(self):
        parsed = heuristic_parse("engineers with a phd")
        assert _score_education(_doc(degrees=["doctor of philosophy"]), parsed).score == 1.0
        parsed = heuristic_parse("operators with an mba")
        assert (
            _score_education(
                _doc(degrees=["master of business administration"]), parsed
            ).score
            == 1.0
        )


# --------------------------------------------------------------------------
# a canonicalised alias is not also a keyword
# --------------------------------------------------------------------------


class TestAliasesAreNotAlsoKeywords:
    def test_a_location_alias_is_not_scored_as_a_missing_keyword(self):
        parsed = heuristic_parse("AI engineers in NYC with pytorch")
        assert parsed.locations == ["new york"]
        assert "nyc" not in parsed.keywords

    def test_ai_is_a_function_not_an_employer_industry(self):
        assert heuristic_parse("AI engineers in NYC with pytorch").industries == []

    def test_the_spelled_out_industry_still_resolves(self):
        assert heuristic_parse("artificial intelligence companies in nyc").industries == [
            "ai"
        ]

    def test_an_industry_alias_is_not_scored_as_a_missing_keyword(self):
        parsed = heuristic_parse("artificial intelligence companies in nyc")
        assert not ({"artificial", "intelligence"} & set(parsed.keywords))

    def test_a_degree_alias_is_not_scored_as_a_missing_keyword(self):
        parsed = heuristic_parse("backend engineers with a masters degree")
        assert not ({"masters", "degree"} & set(parsed.keywords))

    def test_a_negated_location_is_neither_a_target_nor_a_keyword(self):
        parsed = heuristic_parse("backend engineers, not in London")
        assert "london" not in parsed.locations
        assert "london" not in parsed.keywords


# --------------------------------------------------------------------------
# remote
# --------------------------------------------------------------------------


class TestRemotePreference:
    """``remote_ok=False`` means "in the office". Scoring the candidate's flag
    without reading the request handed a perfect 1.0 to exactly the people an
    onsite search is trying to rule out, at weight 1.5."""

    def test_an_onsite_search_does_not_reward_remote_only_people(self):
        parsed = ParsedQuery(remote_ok=False)
        remote = _score_location(_doc(open_to_remote=True), parsed)
        onsite = _score_location(_doc(open_to_remote=False), parsed)
        assert onsite.score > remote.score
        assert onsite.score == 1.0 and remote.score == 0.35

    def test_a_remote_search_still_rewards_remote_people(self):
        parsed = ParsedQuery(remote_ok=True)
        assert _score_location(_doc(open_to_remote=True), parsed).score == 1.0
        assert _score_location(_doc(open_to_remote=False), parsed).score == 0.35

    def test_no_stated_preference_scores_no_location_criterion(self):
        assert _score_location(_doc(), ParsedQuery()) is None

    def test_a_stated_place_still_wins_over_the_remote_flag(self):
        criterion = _score_location(_doc(), ParsedQuery(locations=["new york"], remote_ok=False))
        assert criterion.kind == "location" and criterion.score > 0.75


# --------------------------------------------------------------------------
# the Postgres hard-filter SQL
# --------------------------------------------------------------------------


class TestPostgresEligibilitySql:
    """SQLite never runs this SQL, so both of these shipped invisibly."""

    def _run(self, parsed: ParsedQuery) -> _RecordingSession:
        db = _RecordingSession()
        PostgresIndex(db, "org-1")._eligible(parsed)
        return db

    def test_company_exclusions_compare_normalised_columns(self):
        db = self._run(ParsedQuery(excluded_companies=["Acme"]).normalized())
        assert "current_company_normalized = ANY" in db.sql
        assert "e.company_normalized = ANY" in db.sql
        # Comparing against the raw display text is what let "Acme Inc." through.
        assert "lower(trim(c.current_company))" not in db.sql
        assert db.params["excl_companies"] == ["acme"]

    def test_a_corporate_suffix_normalises_to_the_same_key(self):
        # Which is why matching the derived column is enough: the flush
        # listener stamps candidates through the same function.
        assert tx.normalize_company("Acme Inc.") == "acme"
        assert tx.normalize_company("Acme") == "acme"

    def test_a_multi_word_negative_is_bound_as_a_phrase_tsquery(self):
        db = self._run(heuristic_parse("engineers not in the US"))
        assert db.params["neg_tsquery"] == "(in <-> the <-> us)"

    def test_the_in_process_filter_agrees_about_the_suffix(self):
        docs = [_doc(current_company="Acme Inc.")]
        parsed = ParsedQuery(excluded_companies=["Acme"]).normalized()
        outcome = apply_filters(docs, parsed)
        assert outcome.excluded.get("excluded_company") == 1
        assert not np.any(outcome.mask)
