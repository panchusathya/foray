"""Pasting a job description into search.

The regression these cover: ``/v1/search`` used to hand the whole paste to
``heuristic_parse``, which put every tool the document mentioned into
``must_have_skills``. The hard-miss penalty in ``assess`` then multiplied
every candidate by the same floor, so the scores collapsed into a narrow band
and the ordering became retrieval noise.
"""

from __future__ import annotations

from juicebox.repository import upsert_candidates
from juicebox.search.index import get_index
from juicebox.search.jd import (
    MAX_MUST_HAVES,
    heuristic_jd_parse,
    infer_location,
    looks_like_job_description,
)
from juicebox.search.parser import heuristic_parse, parse_query
from juicebox.search.scoring import assess

JD = """Senior Machine Learning Engineer
Acme Robotics - San Francisco, CA (Hybrid)

About Acme
Acme Robotics is a Series B company backed by top-tier investors. We raised $120
million to build autonomous warehouse systems.

The Role
We are looking for a Senior Machine Learning Engineer to join our Perception team.

What you'll do
- Design, train and evaluate deep learning models using PyTorch
- Deploy models to production on Kubernetes

Requirements
- 5+ years of industry experience in machine learning
- Strong Python and PyTorch

Nice to have
- Exposure to AWS, Docker, Terraform, Kafka, Spark, Airflow, Snowflake, dbt

Benefits
- Competitive salary and equity
- 401(k) match
- Unlimited PTO

Acme Robotics is an equal opportunity employer. We do not sponsor visas.
No agencies please.
"""


class TestDetection:
    def test_a_search_sentence_is_not_a_job_description(self):
        assert not looks_like_job_description(
            "staff backend engineers at fintech companies who know Go and Kafka, "
            "ideally payments experience, SF or remote, 8+ years"
        )

    def test_a_pasted_posting_is(self):
        assert looks_like_job_description(JD)

    def test_long_prose_without_posting_shape_is_not(self):
        # A recruiter can paste a paragraph of context and still be asking a
        # question; only headings or a bulleted list make it a posting.
        assert not looks_like_job_description(
            "I need someone who has scaled a payments team. " * 20
        )


class TestCompilation:
    def test_must_haves_are_capped(self):
        parsed = heuristic_jd_parse(JD)
        assert len(parsed.must_have_skills) <= MAX_MUST_HAVES

    def test_nice_to_have_section_does_not_become_required(self):
        parsed = heuristic_jd_parse(JD)
        for skill in ("terraform", "snowflake", "dbt", "airflow"):
            assert skill not in parsed.must_have_skills
            assert skill in parsed.nice_to_have_skills

    def test_boilerplate_does_not_become_a_hard_filter(self):
        # "We do not sponsor visas" parsed as a negative keyword, which is a
        # hard filter -- it silently removed people.
        parsed = heuristic_jd_parse(JD)
        assert parsed.negative_keywords == []


    def test_company_blurb_does_not_become_keywords(self):
        parsed = heuristic_jd_parse(JD)
        assert "investors" not in parsed.keywords
        assert "acme" not in parsed.keywords

    def test_funding_is_not_read_as_salary(self):
        # "$120 million" sits in the company blurb, not the pay clause.
        parsed = heuristic_jd_parse(JD)
        assert parsed.max_compensation is None or parsed.max_compensation < 10_000_000


    def test_infer_location_ignores_the_body(self):
        # Offices named far down the posting are not the role's location.
        text = "Backend Engineer\nRemote - London\n\nAbout us\nWe have offices in Berlin.\n"
        assert "berlin" not in infer_location(text).lower()


class TestRouting:
    def test_parse_query_routes_a_paste_to_the_jd_compiler(self):
        _, parser = parse_query(JD, use_llm=False)
        assert parser == "heuristic_jd"


class TestRankingRecovery:
    """The point of all of it: a JD has to separate good from bad."""

    def _docs(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        session.commit()
        return get_index(session, account["org_id"]).docs

    def test_the_paste_no_longer_scores_like_a_sentence(
        self, session, account, sample_candidates
    ):
        """The regression, stated as the difference between the two readings.

        A fixed threshold would be arbitrary -- the right score depends on how
        many of the listed tools this particular corpus happens to know. What
        must hold is that reading the JD as a JD separates people, and reading
        it as a sentence does not.
        """
        docs = self._docs(session, account, sample_candidates)
        as_jd, _ = parse_query(JD, use_llm=False)
        as_sentence = heuristic_parse(JD)

        jd_scores = sorted((assess(d, as_jd).score for d in docs), reverse=True)
        sentence_scores = sorted((assess(d, as_sentence).score for d in docs), reverse=True)

        # Reading it as a sentence makes every skill in the document a hard
        # requirement, so the penalty in ``assess`` caps even the person the
        # role was written for at a score no recruiter would trust.
        assert sentence_scores[0] < 0.3
        # Reading it as a JD lifts that person clear of the floor, and puts
        # real distance between them and the rest.
        assert jd_scores[0] > 2 * sentence_scores[0]
        assert (jd_scores[0] - jd_scores[-1]) > 0.25

    def test_the_right_person_beats_the_adjacent_one(
        self, session, account, sample_candidates
    ):
        docs = self._docs(session, account, sample_candidates)
        parsed, _ = parse_query(JD, use_llm=False)
        scored = {d.full_name: assess(d, parsed).score for d in docs}
        ada = scored["Ada Okonkwo"]  # senior ML engineer, SF, pytorch
        others = [v for k, v in scored.items() if k != "Ada Okonkwo"]
        assert all(ada > other for other in others), scored
