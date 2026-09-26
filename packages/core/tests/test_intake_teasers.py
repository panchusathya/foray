"""The end-of-call pitch: composed lines, preference-only ranking, honest promises.

Three properties carry the feature and each gets tested hard:

**Everything speakable is composed in code.** The lines, the count, the
follow-up promise — a company name or a pay figure the model invents on a
recorded call is the claim this feature cannot afford, so the server's output
is asserted verbatim.

**Selection reads only what the caller asked for.** Ranking runs
``preference_term`` against the live record — never the candidate-fact scoring
recruiters see — so the intake channel keeps evaluating nobody, and a fresh
caller needs no ``JobMatch`` rows to get a pitch.

**A promise that cannot be kept lands on a human.** The close only offers a
text when something can send one, and an opted-in caller nothing can text is
parked on the review queue rather than forgotten.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from juicebox.intake import teasers
from juicebox.intake.providers import RetellProvider, set_voice_provider
from juicebox.intake.teasers import (
    GUIDANCE_NOT_READY,
    _comp_phrase,
    _teaser_line,
    rank_postings,
)
from juicebox.models import (
    Candidate,
    Event,
    IntakeCall,
    IntakeNumber,
    JobMatch,
    JobPosting,
)
from juicebox.repository import sync_candidate_phones

OUR_NUMBER = "+16283865454"
CANDIDATE_NUMBER = "+14155550123"
API_KEY = "key_test_signing_secret"

TRANSCRIPT = (
    "Agent: This call is recorded, is that OK?\n"
    "Candidate: Yes.\n"
    "Agent: What are you looking for?\n"
    "Candidate: A senior machine learning engineer role in San Francisco.\n"
)


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv("RETELL_API_KEY", API_KEY)
    from juicebox.config import reset_settings_cache

    reset_settings_cache()
    set_voice_provider(None)
    yield
    set_voice_provider(None)


@pytest.fixture
def signer():
    return RetellProvider(api_key=API_KEY)


@pytest.fixture
def owned_number(session, account):
    row = IntakeNumber(
        org_id=account["org_id"], provider="retell", phone=OUR_NUMBER, label="intake line"
    )
    session.add(row)
    session.commit()
    return row


def call_payload(event="call_started", **overrides):
    call = {
        "call_id": "call_abc123",
        "call_type": "phone_call",
        "call_status": "ongoing",
        "direction": "inbound",
        "from_number": CANDIDATE_NUMBER,
        "to_number": OUR_NUMBER,
        "agent_id": "agent_1",
        "start_timestamp": 1714608475945,
        "transcript": TRANSCRIPT,
        "retell_llm_dynamic_variables": {},
        "metadata": {},
    }
    call.update(overrides)
    return {"event": event, "call": call}


def function_call(transcript=TRANSCRIPT, call_id="call_abc123"):
    return {
        "name": "preview_matches",
        "call": {"call_id": call_id, "transcript": transcript, "agent_id": "agent_1"},
        "args": {},
    }


def post(client, signer, payload, *, path="/v1/intake/matches"):
    raw = json.dumps(payload).encode()
    header = signer.sign(raw_body=raw)
    return client.post(
        path,
        content=raw,
        headers={"X-Retell-Signature": header, "Content-Type": "application/json"},
    )


_POSTING_SEQ = iter(range(1, 10_000))


def _posting(session, org_id, **overrides):
    fields = {
        "org_id": org_id,
        "source": "exa",
        "source_id": f"src-{next(_POSTING_SEQ)}",
        "company": "Stripe",
        "company_normalized": "stripe",
        "title": "Machine Learning Engineer",
        "title_normalized": "machine learning engineer",
        "location": "San Francisco, CA",
        "seniority": "senior",
        "status": "open",
        "parsed_spec": {
            "job_titles": ["machine learning engineer"],
            "seniority": ["senior"],
            "locations": ["san francisco"],
            "must_have_skills": ["pytorch"],
            "max_compensation": 300000,
        },
    }
    fields.update(overrides)
    row = JobPosting(**fields)
    session.add(row)
    session.commit()
    return row


def _filled_required() -> dict:
    from juicebox.intake import empty_record

    record = empty_record()
    record["spec"].update(
        {
            "job_titles": ["machine learning engineer"],
            "seniority": ["senior"],
            "locations": ["san francisco"],
            "remote_ok": False,
            "must_have_skills": ["pytorch"],
        }
    )
    record.update(
        {
            "employment_status": "employed_searching",
            "timeline": "under_1_month",
            "target_base": 240000.0,
            "work_authorization": "green card",
            "active_processes": [{"company": "none", "stage": "none"}],
            "ok_to_share_with_clients": True,
        }
    )
    return record


def _sparse_record() -> dict:
    from juicebox.intake import empty_record

    record = empty_record()
    record["spec"]["job_titles"] = ["machine learning engineer"]
    return record


def _stub_extraction(monkeypatch, record):
    from juicebox.intake.extract import ExtractionResult
    from juicebox.intake.schema import coverage

    # `live_call` is accepted because production passes it: the pitch runs
    # while the caller waits, so it takes the voice budget. A stub that does
    # not accept it raises TypeError inside `teaser_snapshot`, whose catch-all
    # turns that into the ordinary close -- so the tests go green-to-red in the
    # confusing direction, asserting fallback guidance rather than a signature
    # mismatch.
    def _extract(transcript, *, known="", fast=True, live_call=False):
        return ExtractionResult(record=record, extractor="llm", coverage=coverage(record))

    monkeypatch.setattr(teasers, "extract_intake", _extract)


# --------------------------------------------------------------------------
# line composition: the exact sentences a caller hears
# --------------------------------------------------------------------------


class TestLineComposition:
    def test_a_mandate_posting_is_never_named(self, session, account):
        posting = _posting(
            session, account["org_id"], source="project", company="SecretCo", parsed_spec={}
        )
        line = _teaser_line(posting)
        assert "SecretCo" not in line
        assert "one of our client companies" in line

    def test_pay_is_quoted_only_as_the_posting_states_it(self):
        assert _comp_phrase({"min_compensation": 200000}) == "paying upwards of $200,000"
        assert _comp_phrase({"max_compensation": 250000}) == "paying up to $250,000"
        assert _comp_phrase({}) == ""

    def test_a_non_dollar_range_is_not_read_as_dollars(self):
        phrase = _comp_phrase({"max_compensation": 90000, "compensation_currency": "GBP"})
        assert phrase == ""


# --------------------------------------------------------------------------
# ranking: only what the caller asked for, and never a role we should not pitch
# --------------------------------------------------------------------------


class TestRanking:
    def test_ranking_reads_only_stated_preferences(self, session, account):
        good = _posting(session, account["org_id"])
        _posting(
            session,
            account["org_id"],
            company="General Hospital",
            company_normalized="general hospital",
            title="Registered Nurse",
            title_normalized="registered nurse",
            location="Miami, FL",
            seniority="",
            parsed_spec={},
        )
        ranked = rank_postings(db=session, org_id=account["org_id"], record=_filled_required())
        assert [posting.id for posting, _ in ranked] == [good.id]

    def test_an_excluded_company_is_never_pitched(self, session, account):
        _posting(session, account["org_id"], company="Meta", company_normalized="meta")
        record = _filled_required()
        record["spec"]["excluded_companies"] = ["Meta"]
        assert rank_postings(db=session, org_id=account["org_id"], record=record) == []

    def test_a_ceiling_under_their_floor_is_never_pitched(self, session, account):
        _posting(
            session,
            account["org_id"],
            parsed_spec={
                "job_titles": ["machine learning engineer"],
                "locations": ["san francisco"],
                "max_compensation": 150000,
            },
        )
        assert (
            rank_postings(db=session, org_id=account["org_id"], record=_filled_required()) == []
        )

    def test_a_posting_their_recruiter_dismissed_is_never_pitched(self, session, account):
        posting = _posting(session, account["org_id"])
        person = _candidate(session, account["org_id"])
        session.add(
            JobMatch(
                org_id=account["org_id"],
                job_posting_id=posting.id,
                candidate_id=person.id,
                score=0.9,
                status="dismissed",
            )
        )
        session.commit()
        assert (
            rank_postings(
                db=session,
                org_id=account["org_id"],
                record=_filled_required(),
                candidate_id=person.id,
            )
            == []
        )

    def test_their_current_employer_is_never_pitched(self, session, account):
        _posting(session, account["org_id"])
        person = _candidate(session, account["org_id"], current_company="Stripe")
        assert (
            rank_postings(
                db=session,
                org_id=account["org_id"],
                record=_filled_required(),
                candidate_id=person.id,
            )
            == []
        )

    def test_a_closed_posting_is_never_pitched(self, session, account):
        _posting(session, account["org_id"], status="closed")
        assert (
            rank_postings(db=session, org_id=account["org_id"], record=_filled_required()) == []
        )


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------


class TestMatchesEndpoint:
    def test_it_requires_a_signature(self, client):
        response = client.post("/v1/intake/matches", json=function_call())
        assert response.status_code == 401


    def test_an_unfinished_gate_pitches_nothing(
        self, client, session, signer, owned_number, monkeypatch
    ):
        _stub_extraction(monkeypatch, _sparse_record())
        _posting(session, owned_number.org_id)
        post_webhook(client, signer)
        response = post(client, signer, function_call())
        body = response.json()
        assert body["lines"] == []
        assert body["guidance"] == GUIDANCE_NOT_READY

    def test_a_finished_call_gets_lines_and_records_the_offer(
        self, client, session, signer, owned_number, monkeypatch
    ):
        _stub_extraction(monkeypatch, _filled_required())
        for _ in range(3):
            _posting(session, owned_number.org_id)
        post_webhook(client, signer)
        response = post(client, signer, function_call())
        body = response.json()
        assert body["matches"] == 3
        assert len(body["lines"]) == 2
        assert body["lines"][0].startswith("a Machine Learning Engineer role at Stripe")
        assert body["more_line"] == (
            "There is one more in our database that fits what you described."
        )
        assert "ask what they think about these opportunities" in body["guidance"]
        assert "start_text_verification" in body["guidance"]

        session.expire_all()
        call = session.scalar(select(IntakeCall))
        offered = call.matches_offered or []
        assert len(offered) == 2
        assert offered[0]["line"] == body["lines"][0]
        assert offered[0]["posting_id"]
        event = session.scalar(select(Event).where(Event.type == "intake.matches_offered"))
        assert event is not None
        assert event.payload["matched"] == 3

    def test_the_kill_switch_returns_the_ordinary_close(
        self, client, session, signer, owned_number, monkeypatch
    ):
        monkeypatch.setenv("INTAKE_TEASERS_ENABLED", "false")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        _stub_extraction(monkeypatch, _filled_required())
        _posting(session, owned_number.org_id)
        post_webhook(client, signer)
        response = post(client, signer, function_call())
        body = response.json()
        assert body["lines"] == []
        assert "may close" in body["guidance"]
        session.expire_all()
        call = session.scalar(select(IntakeCall))
        assert (call.matches_offered or []) == []


def post_webhook(client, signer):
    """Create the call row the way production does: through the webhook."""
    response = post(
        client, signer, call_payload(event="call_started"), path="/v1/intake/webhook"
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------
# extraction must not queue cards before the text number is verified; the
# live verification and post-reply delivery tests live in test_live_text_verification.py
# --------------------------------------------------------------------------


class TestUndeliverablePromise:
    def _call(self, session, org_id, *, opt_in=True, offered=True, candidate_id=None):
        row = IntakeCall(
            org_id=org_id,
            provider="retell",
            provider_call_id=f"call_{'a' if opt_in else 'b'}{'x' if offered else 'y'}",
            entry_point="dial_in",
            from_number=CANDIDATE_NUMBER,
            to_number=OUR_NUMBER,
            status="completed",
            transcript=TRANSCRIPT,
            sms_opt_in=opt_in,
            matches_offered=(
                [{"posting_id": "jpost_1", "line": "a role", "score": 0.9}] if offered else []
            ),
            candidate_id=candidate_id,
        )
        session.add(row)
        session.commit()
        return row

    def _extract(self, session, call, monkeypatch):
        from juicebox.intake import service

        def _stub(transcript, *, known="", fast=True):
            from juicebox.intake.extract import ExtractionResult
            from juicebox.intake.schema import coverage

            record = _filled_required()
            return ExtractionResult(record=record, extractor="llm", coverage=coverage(record))

        monkeypatch.setattr(service, "extract_intake", _stub)
        service.extract_call(session, call)
        session.commit()

    def _handoff_jobs(self, session):
        from juicebox.jobs import KIND_INTAKE_MATCH_TEXT
        from juicebox.models import Job

        return session.scalars(
            select(Job).where(Job.kind == KIND_INTAKE_MATCH_TEXT)
        ).all()

    def test_extraction_does_not_queue_cards_before_the_reply(self, session, account, monkeypatch):
        person = _candidate(session, account["org_id"], phones=[CANDIDATE_NUMBER])
        call = self._call(session, account["org_id"], candidate_id=person.id)
        self._extract(session, call, monkeypatch)
        assert self._handoff_jobs(session) == []


def _candidate(session, org_id, *, phones=None, current_company=""):
    person = Candidate(
        org_id=org_id,
        source="test",
        source_id="c1",
        full_name="Ada Okonkwo",
        phones=phones or [],
        current_company=current_company,
    )
    session.add(person)
    session.flush()
    sync_candidate_phones(session, person)
    session.commit()
    return person
