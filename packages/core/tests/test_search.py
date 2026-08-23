import numpy as np
import pytest

from juicebox.llm.embeddings import HashingEmbedder, cosine_similarity, tokenize
from juicebox.repository import upsert_candidates
from juicebox.search.bm25 import BM25Index
from juicebox.search.engine import get_engine
from juicebox.search.fusion import (
    normalize_scores,
    rank_positions,
    reciprocal_rank_fusion,
)
from juicebox.search.index import get_index
from juicebox.search.parser import ParsedQuery, heuristic_parse, parse_compensation
from juicebox.search.scoring import assess, requirement_list

# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


class TestHeuristicParser:
    def test_extracts_title_skills_seniority_and_location(self):
        parsed = heuristic_parse(
            "senior machine learning engineers in San Francisco with PyTorch, 5+ years"
        )
        assert "machine learning engineer" in parsed.job_titles
        assert "pytorch" in parsed.must_have_skills
        assert parsed.seniority == ["senior"]
        assert parsed.locations == ["san francisco"]
        assert parsed.min_years_experience == 5

    def test_year_ranges(self):
        assert heuristic_parse("engineers with 3-6 years").min_years_experience == 3
        assert heuristic_parse("engineers with 3-6 years").max_years_experience == 6
        assert heuristic_parse("under 4 years of experience").max_years_experience == 4

    def test_negated_location_is_not_treated_as_a_target(self):
        parsed = heuristic_parse("backend engineers, not in London")
        assert "london" not in parsed.locations

    def test_recruiting_boilerplate_never_becomes_a_keyword(self):
        parsed = heuristic_parse(
            "looking for strong senior engineers with experience who know Go"
        )
        assert not ({"looking", "strong", "experience", "know", "engineers"} & set(parsed.keywords))
        assert "go" in parsed.must_have_skills

    def test_remote_detection(self):
        assert heuristic_parse("remote product designers").remote_ok is True
        assert heuristic_parse("product designers in Berlin").remote_ok is None

    def test_plural_titles_match(self):
        assert "product manager" in heuristic_parse("product managers in NYC").job_titles

    def test_overrides_beat_the_sentence(self):
        parsed = heuristic_parse("engineers in London")
        merged = parsed.merge_overrides({"locations": ["berlin"], "seniority": ["staff"]})
        assert merged.locations == ["berlin"]
        assert merged.seniority == ["staff"]

    def test_round_trips_through_dict(self):
        parsed = heuristic_parse("staff go engineers in Berlin")
        assert ParsedQuery.from_dict(parsed.to_dict()).to_dict() == parsed.to_dict()


# --------------------------------------------------------------------------
# retrieval primitives
# --------------------------------------------------------------------------


class TestStatedCompensation:
    """``jobmatch.preference`` vetoes on the ceiling, so a false positive here
    silently drops a real role. These cases are the ones that cost us that."""

    def test_a_range_fills_both_ends(self):
        assert parse_compensation("Base salary 180,000 to 220,000 USD.") == (
            180000.0,
            220000.0,
            "USD",
        )

    def test_a_single_figure_fills_both_ends(self):
        low, high, _ = parse_compensation("The salary is $200,000 per year.")
        assert (low, high) == (200000.0, 200000.0)

    def test_up_to_states_a_ceiling_only(self):
        assert parse_compensation("Compensation: up to £150k.") == (None, 150000.0, "GBP")

    def test_at_least_states_a_floor_only(self):
        assert parse_compensation("Salary at least 180k.") == (180000.0, None, "")

    def test_a_401k_match_is_not_pay(self):
        assert parse_compensation("Competitive salary, 401k match, unlimited PTO.") == (
            None,
            None,
            "",
        )

    def test_a_funding_round_is_not_pay(self):
        assert parse_compensation("We raised $200 million. Salary is competitive.") == (
            None,
            None,
            "",
        )

    def test_revenue_is_not_pay(self):
        assert parse_compensation("A $5B revenue business with a great salary.") == (
            None,
            None,
            "",
        )

    def test_a_clause_with_no_pay_cue_is_ignored(self):
        assert parse_compensation("Offices in 12 cities and 40,000 customers.") == (
            None,
            None,
            "",
        )

    def test_competitive_salary_is_not_a_figure(self):
        assert parse_compensation("Competitive salary and equity.") == (None, None, "")

    def test_the_band_carries_through_normalisation(self):
        parsed = ParsedQuery(
            min_compensation=180000.0, max_compensation=220000.0, compensation_currency="usd"
        ).normalized()
        assert parsed.max_compensation == 220000.0
        assert parsed.compensation_currency == "USD"
        # normalized() rebuilds every field by hand; an omission drops it silently.
        assert ParsedQuery.from_dict(parsed.to_dict()).min_compensation == 180000.0


class TestBM25:
    def test_ranks_the_document_containing_the_query_term_first(self):
        docs = [
            tokenize("python machine learning engineer pytorch"),
            tokenize("frontend react typescript designer"),
            tokenize("python data engineer spark airflow"),
        ]
        index = BM25Index(docs)
        scores = index.score(tokenize("pytorch"))
        assert scores[0] > scores[2]
        assert scores[1] == 0.0

    def test_empty_corpus_is_safe(self):
        assert BM25Index([]).score(["python"]).size == 0

    def test_unknown_terms_score_zero(self):
        index = BM25Index([tokenize("python engineer")])
        assert index.score(tokenize("cobol")).tolist() == [0.0]


class TestEmbeddings:
    def test_deterministic_and_normalised(self):
        embedder = HashingEmbedder(dim=128)
        a = embedder.embed_one("senior machine learning engineer")
        b = embedder.embed_one("senior machine learning engineer")
        assert np.allclose(a, b)
        assert pytest.approx(float(np.linalg.norm(a)), abs=1e-5) == 1.0

    def test_related_text_scores_higher_than_unrelated(self):
        embedder = HashingEmbedder(dim=512)
        query = embedder.embed_one("machine learning engineer pytorch")
        matrix = embedder.embed(
            [
                "senior machine learning engineer with pytorch",
                "account executive selling enterprise software",
            ]
        )
        scores = cosine_similarity(matrix, query)
        assert scores[0] > scores[1]

    def test_empty_text_yields_zero_vector(self):
        assert not HashingEmbedder(dim=32).embed_one("").any()


class TestFusion:
    def test_rank_positions_skips_masked_and_zero(self):
        scores = np.array([0.9, 0.0, 0.5])
        mask = np.array([True, True, False])
        assert rank_positions(scores, mask) == {0: 1}

    def test_rrf_rewards_appearing_in_both_lists(self):
        fused = reciprocal_rank_fusion([{0: 1, 1: 2}, {1: 1, 0: 5}], k=10)
        assert fused[1] > fused[0]

    def test_normalize_scores_spans_zero_to_one(self):
        normalized = normalize_scores({0: 1.0, 1: 3.0, 2: 2.0})
        assert normalized[0] == 0.0 and normalized[1] == 1.0

    def test_mismatched_weights_raise(self):
        with pytest.raises(ValueError):
            reciprocal_rank_fusion([{0: 1}], weights=[1.0, 1.0])


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


class TestScoring:
    def _doc(self, session, account, sample_candidates, index_of=0):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        session.commit()
        return get_index(session, account["org_id"]).docs

    def test_exact_match_scores_high_with_evidence(
        self, session, account, sample_candidates
    ):
        docs = self._doc(session, account, sample_candidates)
        ada = next(d for d in docs if d.full_name == "Ada Okonkwo")
        parsed = heuristic_parse(
            "senior machine learning engineer in San Francisco with pytorch"
        )
        assessment = assess(ada, parsed)
        assert assessment.score > 0.8
        titles = [c for c in assessment.criteria if c.kind == "title"]
        assert titles and titles[0].status == "strong"
        assert all(c.evidence for c in assessment.criteria)

    def test_missing_hard_skill_is_penalised(self, session, account, sample_candidates):
        docs = self._doc(session, account, sample_candidates)
        bo = next(d for d in docs if d.full_name == "Bo Lindqvist")
        parsed = heuristic_parse("frontend engineer with rust and kafka")
        assessment = assess(bo, parsed)
        misses = {c.label for c in assessment.misses}
        assert "Must have rust" in misses
        assert assessment.score < 0.7

    def test_no_criteria_returns_neutral_score(self, session, account, sample_candidates):
        docs = self._doc(session, account, sample_candidates)
        assessment = assess(docs[0], ParsedQuery())
        assert assessment.score == 0.5
        assert assessment.criteria == []

    def test_requirement_list_is_human_readable(self):
        parsed = heuristic_parse("staff go engineers in Berlin with 8+ years")
        requirements = requirement_list(parsed)
        assert any("Berlin".lower() in r.lower() for r in requirements)
        assert any("8" in r for r in requirements)


# --------------------------------------------------------------------------
# end-to-end engine
# --------------------------------------------------------------------------


class TestEngine:
    def _load(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        session.commit()

    def test_ranks_the_intended_person_first(self, session, account, sample_candidates):
        self._load(session, account, sample_candidates)
        outcome = get_engine().search(
            session,
            org_id=account["org_id"],
            query="senior machine learning engineer in San Francisco with pytorch and llm",
            limit=3,
            persist=False,
        )
        assert outcome.results[0].doc.full_name == "Ada Okonkwo"
        assert outcome.results[0].rank == 1
        assert outcome.reranked is False  # no model configured in tests

    def test_hard_filter_removes_excluded_companies(
        self, session, account, sample_candidates
    ):
        self._load(session, account, sample_candidates)
        outcome = get_engine().search(
            session,
            org_id=account["org_id"],
            query="backend engineer",
            overrides={"excluded_companies": ["ramp"]},
            persist=False,
        )
        assert all(r.doc.full_name != "Cleo Rahman" for r in outcome.results)
        assert outcome.excluded.get("excluded_company") == 1

    def test_min_years_filter_applies_with_tolerance(
        self, session, account, sample_candidates
    ):
        self._load(session, account, sample_candidates)
        outcome = get_engine().search(
            session,
            org_id=account["org_id"],
            query="engineers with 20+ years of experience",
            persist=False,
        )
        assert outcome.results == []
        assert outcome.total_matched == 0

    def test_empty_corpus_returns_empty_result(self, session, account):
        outcome = get_engine().search(
            session, org_id=account["org_id"], query="anyone", persist=False
        )
        assert outcome.results == [] and outcome.total_matched == 0

    def test_persisting_writes_search_and_results(
        self, session, account, sample_candidates
    ):
        self._load(session, account, sample_candidates)
        outcome = get_engine().search(
            session,
            org_id=account["org_id"],
            query="frontend engineer with react",
            persist=True,
        )
        session.commit()
        assert outcome.search_id is not None

        from juicebox.models import SearchResult

        rows = session.query(SearchResult).filter_by(search_id=outcome.search_id).all()
        assert len(rows) == len(outcome.results)
        assert rows[0].evidence["criteria"]

    def test_index_cache_invalidates_on_write(self, session, account, sample_candidates):
        self._load(session, account, sample_candidates)
        assert len(get_index(session, account["org_id"])) == 3

        upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[{"source": "test", "source_id": "new-1", "full_name": "Dee Wu"}],
        )
        session.commit()
        assert len(get_index(session, account["org_id"])) == 4
