"""Why a text-onboarded candidate got "nothing new clears the bar".

The live thread on 2026-08-25 finished onboarding, asked "find me roles", and
was told nothing cleared the bar. Exa was reachable and the spec was complete.
Four separate breaks sat between the two facts, and each one is pinned here:

1. discovery queued its parse jobs under the wrong payload key, so every
   posting it pulled sat unparsed and therefore unmatched,
2. the preference term compared locations by string equality, so "San
   Francisco Bay Area" -- what LinkedIn writes on every Bay Area profile --
   missed a role in "San Francisco, CA",
3. the years of experience the candidate gave us in the second message never
   reached the candidate row, so every posting stating a minimum scored them
   at "0 years of tracked experience",
4. "find me roles" only ever read the book. It never went looking, so a
   candidate whose spec had been searched once -- at handover, before any of
   it was parsed -- could ask forever and never trigger another search.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.jobmatch.discover_candidate import (
    enqueue_discovery_on_request,
    handle_discover_one,
)
from juicebox.jobmatch.exa import ExaClient, set_exa
from juicebox.jobmatch.preference import preference_term
from juicebox.jobs import KIND_JOBS_DISCOVER_ONE, KIND_JOBS_PARSE
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.conversation import run_turn
from juicebox.messaging.manager import MODE_MANAGING
from juicebox.messaging.threads import open_thread, record_inbound
from juicebox.models import (
    Candidate,
    CandidatePreference,
    Job,
    JobPosting,
    utcnow,
)
from juicebox.search.parser import ParsedQuery
from juicebox.worker import process_job

HITS = [
    {
        "id": "exa-strat-1",
        # A greenhouse-shaped URL so the proposal gate (which only cards roles
        # whose vendor has a submit adapter) resolves it from the URL alone --
        # no network, no `follow` hop, in a test about search-refill semantics.
        "url": "https://boards.greenhouse.io/google/jobs/4012345",
        "title": "Strategic Finance Analyst",
        "text": (
            "Google is hiring a Strategic Finance Analyst in San Francisco, CA. "
            "Financial modelling, planning cycles, product partnering. "
            "1-3 years of experience."
        ),
        "author": "Google",
    }
]


def _exa_transport(calls):
    def transport(_method, url, payload):
        calls.append({"url": url, "payload": payload})
        if url.endswith("/search"):
            _searches.append({"url": url, "payload": payload})
        return {"results": [] if "contents" in url else HITS}

    return transport


@pytest.fixture
def channel():
    fake = FakeChannel(consent=True)
    set_channel(LINQ, fake)
    return fake


@pytest.fixture
def thread(session, account, channel):
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-search",
        entry_point="linq_inbound",
        from_address="+14155550199",
        consent_messaging=True,
    )


def _candidate(session, org_id, **fields) -> Candidate:
    candidate = Candidate(
        org_id=org_id,
        source="test",
        source_id=fields.pop("source_id", "cand-search"),
        full_name="Sathya P",
        current_title="Investor",
        location_city="San Francisco Bay Area",
        **fields,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _preference(session, candidate, **fields) -> CandidatePreference:
    pref = CandidatePreference(
        org_id=candidate.org_id,
        candidate_id=candidate.id,
        spec={
            "job_titles": ["strategic finance"],
            "seniority": ["junior"],
            "locations": ["San Francisco Bay Area"],
            "remote_ok": False,
        },
        **fields,
    )
    session.add(pref)
    session.flush()
    return pref


class TestDiscoveryQueuesParsableJobs:
    """Break 1: the parse jobs discovery queued could never find their posting."""

    def test_every_parse_job_it_queues_names_a_posting_the_handler_can_load(
        self, session, account
    ):
        calls = []
        set_exa(ExaClient(api_key="test", transport=_exa_transport(calls)))
        candidate = _candidate(session, account["org_id"])
        _preference(session, candidate)

        job = Job(
            org_id=account["org_id"],
            kind=KIND_JOBS_DISCOVER_ONE,
            payload={"org_id": account["org_id"], "candidate_id": candidate.id},
            idempotency_key="discover-test",
        )
        session.add(job)
        session.flush()
        result = handle_discover_one(session, job, utcnow())

        assert result["created"] == 1, "the search should have ingested the posting"
        parse_jobs = session.scalars(
            select(Job).where(Job.kind == KIND_JOBS_PARSE)
        ).all()
        assert parse_jobs, "discovery must queue a parse for what it ingested"
        for parse_job in parse_jobs:
            posting_id = (parse_job.payload or {}).get("job_posting_id")
            assert posting_id, f"parse job payload has no job_posting_id: {parse_job.payload}"
            assert session.get(JobPosting, posting_id) is not None

    def test_the_postings_it_finds_actually_reach_open(self, session, account):
        set_exa(ExaClient(api_key="test", transport=_exa_transport([])))
        candidate = _candidate(session, account["org_id"])
        _preference(session, candidate)
        job = Job(
            org_id=account["org_id"],
            kind=KIND_JOBS_DISCOVER_ONE,
            payload={"org_id": account["org_id"], "candidate_id": candidate.id},
            idempotency_key="discover-open",
        )
        session.add(job)
        session.flush()
        handle_discover_one(session, job, utcnow())

        for _ in range(20):
            pending = session.scalar(
                select(Job).where(Job.status == "queued", Job.kind == KIND_JOBS_PARSE)
            )
            if pending is None:
                break
            process_job(session, pending)
            session.flush()

        posting = session.scalar(select(JobPosting))
        assert posting is not None
        assert posting.status == "open", "an unparsed posting can never be matched"


class TestLocationsThatMeanTheSamePlace:
    """Break 2: two comparators disagreed about one pair of strings."""

    @pytest.mark.parametrize(
        "posting_location",
        ["San Francisco, CA", "San Francisco", "Menlo Park, CA"],
    )
    def test_a_bay_area_candidate_is_not_a_hard_miss_on_a_bay_area_role(
        self, session, account, posting_location
    ):
        candidate = _candidate(session, account["org_id"])
        pref = _preference(session, candidate)
        posting = JobPosting(
            org_id=account["org_id"],
            source="exa",
            source_id="loc-test",
            title="Strategic Finance Analyst",
            company="Google",
            location=posting_location,
            status="open",
        )
        session.add(posting)
        session.flush()

        _, criteria = preference_term(pref, ParsedQuery.from_dict({}), posting=posting)
        location = next(c for c in criteria if c["name"] == "pref_location")
        assert location["score"] > 0.0, (
            f"{posting_location!r} scored zero against a Bay Area preference"
        )

    def test_a_real_miss_is_still_a_miss(self, session, account):
        candidate = _candidate(session, account["org_id"], source_id="cand-nyc")
        pref = _preference(session, candidate)
        posting = JobPosting(
            org_id=account["org_id"],
            source="exa",
            source_id="loc-nyc",
            title="Strategic Finance Analyst",
            company="Google",
            location="New York, NY",
            status="open",
        )
        session.add(posting)
        session.flush()
        _, criteria = preference_term(pref, ParsedQuery.from_dict({}), posting=posting)
        location = next(c for c in criteria if c["name"] == "pref_location")
        assert location["score"] == 0.0


class TestYearsOfExperienceSurvive:
    """Break 3: the number they gave us in message two never reached matching."""

    def test_the_answer_to_the_years_question_lands_on_the_candidate(
        self, session, account, thread
    ):
        from juicebox.messaging import identity
        from juicebox.messaging.linkedin_flow import update_flow_meta

        thread.contact_email = "years@example.com"
        update_flow_meta(thread, years=1.0)
        session.flush()

        candidate_id = identity.link_candidate(session, thread=thread)
        assert candidate_id
        candidate = session.get(Candidate, candidate_id)
        assert float(candidate.years_experience or 0.0) == 1.0

    def test_a_counted_history_is_not_overwritten_by_a_remembered_one(
        self, session, account, thread
    ):
        from juicebox.messaging import identity
        from juicebox.messaging.linkedin_flow import update_flow_meta

        candidate = _candidate(session, account["org_id"], source_id="cand-years")
        candidate.years_experience = 9.0
        thread.candidate_id = candidate.id
        update_flow_meta(thread, years=1.0)
        session.flush()

        identity.link_candidate(session, thread=thread)
        session.refresh(candidate)
        assert candidate.years_experience == 9.0


class TestAskingGoesLooking:
    """Break 4: "find me roles" read the book and never refilled it."""

    def test_an_empty_book_triggers_a_search_rather_than_a_shrug(
        self, session, account, thread, channel
    ):
        from juicebox.messaging import identity

        set_exa(ExaClient(api_key="test", transport=_exa_transport([])))
        thread.contact_email = "ask@example.com"
        thread.mode = MODE_MANAGING
        session.flush()
        candidate_id = identity.link_candidate(session, thread=thread)
        candidate = session.get(Candidate, candidate_id)
        _preference(session, candidate)

        record_inbound(session, thread=thread, body="find me roles", provider_native_id="ask-1")
        run_turn(session, thread=thread)

        assert "looking now" in channel.bodies[-1].lower()
        queued = session.scalars(
            select(Job).where(Job.kind == KIND_JOBS_DISCOVER_ONE)
        ).all()
        assert len(queued) == 1, "asking must queue exactly one search"

    def test_asking_twice_in_a_day_buys_one_search(self, session, account):
        candidate = _candidate(session, account["org_id"], source_id="cand-twice")
        pref = _preference(session, candidate)
        first = enqueue_discovery_on_request(session, candidate=candidate, preference=pref)
        second = enqueue_discovery_on_request(session, candidate=candidate, preference=pref)
        assert first is not None
        assert second is not None
        assert first.id == second.id, "a second ask must not re-buy the searches"


class TestTheWholeChain:
    """The live thread, end to end: onboarding to a card in the channel.

    Every step is the real one -- the LinkedIn-short flow, the extractor, the
    Exa ingest, the parse, the scorer, the proposal bar. Only the three
    outside services are stood in for: LinkedIn, Exa, and the model.

    The embedder is stood in for too, and that is the point of the last
    assertion rather than an incidental fixture. Production ships
    ``EMBEDDING_PROVIDER=hashing``, whose cosines are noise, and the vector
    term is 0.30 of a 1.40 blend -- so a candidate whose whole profile is a
    LinkedIn header cannot reach ``PROPOSAL_SCORE_FLOOR`` on the other two
    terms alone, however well they fit. This test runs with semantic
    embeddings because that is the configuration in which the product works.
    """

    def test_a_text_onboarded_candidate_gets_a_card(self, session, account, thread, channel):
        import numpy as np

        from juicebox.enrich.linkedin_scrape import LinkedInProfile, set_scraper
        from juicebox.llm import embeddings as embeddings_module
        from juicebox.llm.client import FakeTransport, LLMClient, set_llm
        from juicebox.messaging.linkedin_flow import FLOW_LINKEDIN_SHORT

        class SemanticStub:
            """Everything here is about strategic finance, so everything is close."""

            name = "stub"

            def embed(self, texts, *, input_type="document"):
                rows = []
                for text in texts:
                    finance = float(
                        any(w in text.lower() for w in ("financ", "strateg", "invest"))
                    )
                    rows.append([1.0, finance * 0.9])
                matrix = np.asarray(rows, dtype=np.float32)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                return matrix / norms

        _searches.clear()
        embeddings_module._embedder = SemanticStub()
        set_exa(ExaClient(api_key="test", transport=_exa_transport([])))
        set_llm(LLMClient(transport=FakeTransport(default=_llm_dispatch), enabled=True, model="t"))
        set_scraper(
            lambda url: LinkedInProfile(
                url=url,
                full_name="Sathya P",
                headline="Investor at Westbridge Capital",
                current_title="Investor",
                current_employer="Westbridge Capital",
                location="San Francisco Bay Area",
                experience_years=1.0,
                is_ic=True,
            )
        )
        thread.flow = FLOW_LINKEDIN_SHORT
        session.flush()
        # The proposal gate liveness-checks the URLs about to be carded; a
        # real HEAD to greenhouse.io from a test would 404 a made-up board
        # and close the posting. Everything else about the gate runs real:
        # the greenhouse-shaped HIT URL resolves from the URL alone.
        import juicebox.jobmatch.liveness as liveness

        original_is_gone = liveness.is_gone
        liveness.is_gone = lambda url, **kw: False
        try:
            _turn(session, thread, "hey", "t0")
            _turn(session, thread, "sathya@example.com", "t1")
            _turn(session, thread, "linkedin.com/in/sathya-live", "t2")
            _drain(session)
            run_turn(session, thread=thread)
            _turn(
                session,
                thread,
                "No I'm looking for Strat fin roles at one of the big tech companies",
                "t3",
            )
            _turn(session, thread, "1, 200k, asap", "t4")
            _drain(session)

            pref = session.scalar(select(CandidatePreference))
            assert pref is not None, "onboarding must write a preference"
            assert pref.spec["job_titles"] == ["strategic finance"], (
                "the candidate's correction has to beat the scraped title"
            )
            assert any(
                "strategic finance" in (c["payload"].get("query") or "")
                for c in _searches
            ), "the search has to run on what they asked for"

            _turn(session, thread, "find me roles", "t5")
            # Roles go out one bubble each now, so the cards are a burst and
            # the final message is the "tap the ones you want" prompt.
            body = "\n".join(channel.bodies)
            assert "Strategic Finance Analyst" in body, f"expected a card, got: {body!r}"
            assert "Google" in body
        finally:
            liveness.is_gone = original_is_gone
            embeddings_module.reset_embedder()
            set_scraper(None)
            set_llm(None)


_searches: list[dict] = []


def _turn(session, thread, body, native_id):
    record_inbound(session, thread=thread, body=body, provider_native_id=native_id)
    return run_turn(session, thread=thread)


def _drain(session, limit=120):
    for _ in range(limit):
        job = session.scalar(select(Job).where(Job.status == "queued"))
        if job is None:
            return
        process_job(session, job)
        session.flush()


def _llm_dispatch(kwargs):
    system = str(kwargs.get("system") or "")
    body = "".join(str(m.get("content", "")) for m in kwargs.get("messages", []))
    if system.startswith("You read a scraped web page"):
        return {
            "is_posting": True,
            "title": "Strategic Finance Analyst",
            "company": "Google",
            "location": "San Francisco, CA",
        }
    if system.startswith("You convert a job description"):
        return {
            "job_titles": ["strategic finance analyst"],
            "must_have_skills": ["financial modeling"],
            "seniority": ["junior"],
            "min_years_experience": 1,
            "locations": ["San Francisco"],
            "remote_ok": False,
            "keywords": ["strategic finance"],
        }
    if system.startswith("You read a transcript"):
        return {
            "spec": {
                "job_titles": ["strategic finance"],
                "seniority": ["junior"],
                "locations": ["San Francisco Bay Area"],
                "remote_ok": False,
            },
            "target_base": 200000,
            "timeline": "immediate",
            "work_authorization": "citizen",
            "ok_to_share_with_clients": True,
        }
    del body
    return {}


class TestWhatTheyAskedForOutweighsWhatTheyDid:
    """The blend's centre of gravity, and the guard rail on it.

    A career changer is the case the candidate product exists for. Scored
    mostly on their record they are ranked on the job they are leaving --
    `title` is a flat miss for an investor applying to strategic finance, by
    design, because the criterion reads their current title. Preference now
    outweighs it.

    The guard rail matters as much as the change: a stated title matching a
    posting title, with nothing else behind it, must not be a proposal.
    """

    def _score(self, session, org_id, *, heuristic, vector, preference):
        """Blend three terms the way `score_pair` does, at configured weights."""
        from juicebox.config import get_settings

        s = get_settings()
        weights = [(s.job_match_heuristic_weight, heuristic)]
        if vector is not None:
            weights.append((s.job_match_vector_weight, vector))
        if preference is not None:
            weights.append((s.job_match_preference_weight, preference))
        total = sum(w for w, _ in weights)
        return sum(w * v for w, v in weights) / total

    def test_a_career_changer_clears_the_floor_on_a_role_they_asked_for(
        self, session, account
    ):
        """The live thread's own terms: investor -> strategic finance."""
        from juicebox.config import get_settings

        score = self._score(
            session, account["org_id"], heuristic=0.314, vector=0.097, preference=0.917
        )
        assert score >= get_settings().proposal_score_floor, (
            f"the thread that started this still scores {score:.3f}"
        )

    def test_a_stated_title_alone_is_not_a_proposal(self, session, account):
        """Preference at its maximum, with no corroboration anywhere else.

        Typing a job title must not be enough to put a role in front of
        somebody. Something in the record or the embedding has to agree.
        """
        from juicebox.config import get_settings

        score = self._score(session, account["org_id"], heuristic=0.0, vector=0.0, preference=1.0)
        assert score < get_settings().proposal_score_floor, (
            f"preference alone reached {score:.3f} -- it must not carry a match by itself"
        )

    def test_a_candidate_with_no_preference_is_scored_exactly_as_before(
        self, session, account
    ):
        """The blast radius. `preference_term` returns None for anyone who
        never stated one, and `score_pair` drops absent terms out of the
        blend -- so a recruiter-sourced CRM candidate is untouched by this."""
        from juicebox.jobmatch.engine import score_pair
        from juicebox.search.index import candidate_to_doc
        from juicebox.search.parser import ParsedQuery

        candidate = _candidate(session, account["org_id"], source_id="cand-nopref")
        posting = JobPosting(
            org_id=account["org_id"],
            source="exa",
            source_id="nopref-posting",
            title="Strategic Finance Analyst",
            company="Google",
            location="San Francisco, CA",
            status="open",
        )
        session.add(posting)
        session.flush()

        pair = score_pair(
            candidate_to_doc(candidate),
            ParsedQuery.from_dict(posting.parsed_spec or {}),
            cand_embedding=None,
            cand_embedding_model="",
            posting_embedding=None,
            posting_embedding_model="",
            preference=None,
            posting=posting,
        )
        assert pair.preference_score == 0.0
        assert not any(c.get("kind") == "preference" for c in pair.criteria), (
            "no preference, no preference criteria, no re-weighting"
        )
        assert pair.score == pair.heuristic_score, (
            "with no vector and no preference the blend is the heuristic alone"
        )
