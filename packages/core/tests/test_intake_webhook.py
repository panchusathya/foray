"""The voice intake webhook: signature, org resolution, idempotency, extraction.

These routes are open to the internet and a phone call carries no principal, so
two things carry the whole security model and both are tested hard:

**The signature is the only proof of identity.** Not the payload, not the source
IP. A body that fails verification must be refused before anything is read out
of it.

**The org comes from a row we already own, never from the request.** An org id
taken from a webhook body is a cross-tenant write with a phone bill attached —
the same mistake `AGENTS.md` records against Foray's paid routes.

Plus the boring one that decides whether this works in production at all:
providers retry, so every path has to be idempotent.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from juicebox.intake.providers import RetellProvider, set_voice_provider
from juicebox.intake.service import STATUS_EXTRACTED
from juicebox.jobs import KIND_INTAKE_EXTRACT
from juicebox.models import (
    Candidate,
    CandidatePreference,
    IntakeCall,
    IntakeNumber,
    Job,
)
from juicebox.repository import sync_candidate_phones

OUR_NUMBER = "+18882600142"
CANDIDATE_NUMBER = "+14155550123"
STRANGER_NUMBER = "+14155559999"
API_KEY = "key_test_signing_secret"

TRANSCRIPT = (
    "Agent: This call is recorded, is that OK?\n"
    "Candidate: Yes.\n"
    "Agent: What are you looking for?\n"
    "Candidate: A senior machine learning engineer role in San Francisco.\n"
)


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    """A known signing secret, so tests can produce real signatures."""
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


def call_payload(event="call_ended", **overrides):
    call = {
        "call_id": "call_abc123",
        "call_type": "phone_call",
        "call_status": "ended",
        "direction": "inbound",
        "from_number": CANDIDATE_NUMBER,
        "to_number": OUR_NUMBER,
        "agent_id": "agent_1",
        "start_timestamp": 1714608475945,
        "end_timestamp": 1714608491736,
        "transcript": TRANSCRIPT,
        "recording_url": "https://example.invalid/rec.wav",
        "retell_llm_dynamic_variables": {},
        "metadata": {},
    }
    call.update(overrides)
    return {"event": event, "call": call}


def post(client, signer, payload, *, path="/v1/intake/webhook", timestamp=None, body=None):
    raw = body if body is not None else json.dumps(payload).encode()
    header = signer.sign(raw_body=raw, timestamp_millis=timestamp)
    return client.post(
        path,
        content=raw,
        headers={"X-Retell-Signature": header, "Content-Type": "application/json"},
    )


# --------------------------------------------------------------------------
# signature
# --------------------------------------------------------------------------


class TestSignature:
    def test_a_valid_signature_is_accepted(self, client, signer, owned_number):
        assert post(client, signer, call_payload()).status_code == 200

    def test_a_missing_signature_is_refused(self, client, owned_number):
        assert client.post("/v1/intake/webhook", json=call_payload()).status_code == 401

    def test_a_tampered_body_is_refused(self, client, signer, owned_number):
        """Sign one body, send another. This is the attack the header exists for."""
        raw = json.dumps(call_payload()).encode()
        header = signer.sign(raw_body=raw)
        tampered = json.dumps(call_payload(call_id="call_other")).encode()
        response = client.post(
            "/v1/intake/webhook",
            content=tampered,
            headers={"X-Retell-Signature": header},
        )
        assert response.status_code == 401

    def test_re_serialising_the_body_breaks_verification(self, client, signer, owned_number):
        """Guards the classic implementation error.

        If the handler ever verified against a re-dumped payload instead of the
        raw bytes, this would start passing — and every real signature would
        start failing.
        """
        raw = json.dumps(call_payload(), separators=(",", ":")).encode()
        header = signer.sign(raw_body=raw)
        respaced = json.dumps(json.loads(raw), indent=2).encode()
        response = client.post(
            "/v1/intake/webhook", content=respaced, headers={"X-Retell-Signature": header}
        )
        assert response.status_code == 401

    def test_a_wrong_key_is_refused(self, client, owned_number):
        assert post(client, RetellProvider(api_key="key_wrong"), call_payload()).status_code == 401

    def test_a_stale_timestamp_is_refused(self, client, signer, owned_number):
        """Replay protection: a captured request stops working after 5 minutes."""
        import time

        old = int(time.time() * 1000) - 6 * 60 * 1000
        assert post(client, signer, call_payload(), timestamp=old).status_code == 401

    def test_a_future_timestamp_is_refused(self, client, signer, owned_number):
        import time

        ahead = int(time.time() * 1000) + 6 * 60 * 1000
        assert post(client, signer, call_payload(), timestamp=ahead).status_code == 401

    @pytest.mark.parametrize(
        "header", ["", "garbage", "v=123", "d=abc", "v=abc,d=abc", "v=123,d=nothex!"]
    )
    def test_a_malformed_header_is_refused(self, client, owned_number, header):
        response = client.post(
            "/v1/intake/webhook",
            content=json.dumps(call_payload()).encode(),
            headers={"X-Retell-Signature": header},
        )
        assert response.status_code == 401

    def test_no_configured_key_refuses_everything(
        self, client, signer, owned_number, monkeypatch
    ):
        """Fail closed. Accepting unsigned bodies because no key is set would
        turn a misconfiguration into an open, billable write endpoint."""
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("RETELL_API_KEY", "")
        reset_settings_cache()
        set_voice_provider(None)
        assert post(client, signer, call_payload()).status_code == 401

    def test_a_refused_request_writes_nothing(self, client, session, owned_number):
        client.post("/v1/intake/webhook", json=call_payload())
        assert session.scalars(select(IntakeCall)).all() == []


# --------------------------------------------------------------------------
# org resolution
# --------------------------------------------------------------------------


class TestOrgResolution:
    def test_an_inbound_call_resolves_by_the_number_dialled(
        self, client, session, signer, owned_number, account
    ):
        assert post(client, signer, call_payload()).status_code == 200
        call = session.scalars(select(IntakeCall)).one()
        assert call.org_id == account["org_id"]
        assert call.entry_point == "dial_in"

    def test_an_outbound_call_resolves_by_the_number_we_called_from(
        self, client, session, signer, owned_number, account
    ):
        payload = call_payload(
            direction="outbound", from_number=OUR_NUMBER, to_number=CANDIDATE_NUMBER
        )
        assert post(client, signer, payload).status_code == 200
        call = session.scalars(select(IntakeCall)).one()
        assert call.org_id == account["org_id"]
        assert call.entry_point == "outbound"

    def test_an_unclaimed_number_is_refused(self, client, session, signer, account):
        """No IntakeNumber row, so nobody owns this line. Refuse, never guess."""
        response = post(client, signer, call_payload())
        assert response.status_code == 404
        assert session.scalars(select(IntakeCall)).all() == []

    def test_a_deactivated_number_is_refused(self, client, session, signer, owned_number):
        owned_number.active = False
        session.commit()
        assert post(client, signer, call_payload()).status_code == 404

    def test_the_payload_cannot_choose_the_org(
        self, client, session, signer, owned_number, account
    ):
        """The whole point. A forged org id in metadata must be inert."""
        payload = call_payload()
        payload["call"]["metadata"] = {"org_id": "org_attacker", "orgId": "org_attacker"}
        payload["org_id"] = "org_attacker"
        assert post(client, signer, payload).status_code == 200
        call = session.scalars(select(IntakeCall)).one()
        assert call.org_id == account["org_id"]

    def test_a_web_call_resolves_by_an_existing_row(
        self, client, session, signer, account
    ):
        """Web calls have no numbers. We placed them, so we already own a row."""
        session.add(
            IntakeCall(
                org_id=account["org_id"],
                provider="retell",
                provider_call_id="call_web1",
                entry_point="web",
            )
        )
        session.commit()
        payload = call_payload(
            call_id="call_web1", call_type="web_call", direction="", from_number="", to_number=""
        )
        assert post(client, signer, payload).status_code == 200
        call = session.scalars(select(IntakeCall)).one()
        assert call.org_id == account["org_id"]
        assert call.transcript == TRANSCRIPT

    def test_a_web_call_with_no_prior_row_is_refused(self, client, session, signer, account):
        payload = call_payload(
            call_id="call_web2", call_type="web_call", direction="", from_number="", to_number=""
        )
        assert post(client, signer, payload).status_code == 404


# --------------------------------------------------------------------------
# lifecycle and idempotency
# --------------------------------------------------------------------------


class TestLifecycle:
    def test_call_started_opens_a_row_without_queueing_work(
        self, client, session, signer, owned_number
    ):
        response = post(client, signer, call_payload(event="call_started", transcript=""))
        assert response.json()["extraction_queued"] is False
        call = session.scalars(select(IntakeCall)).one()
        assert call.status == "in_progress"
        assert session.scalars(select(Job).where(Job.kind == KIND_INTAKE_EXTRACT)).all() == []

    def test_call_ended_records_the_transcript_and_queues_extraction(
        self, client, session, signer, owned_number
    ):
        assert post(client, signer, call_payload()).status_code == 200
        call = session.scalars(select(IntakeCall)).one()
        assert call.status == "completed"
        assert call.transcript == TRANSCRIPT
        assert call.recording_url.endswith("rec.wav")
        assert call.duration_seconds == 15
        jobs = session.scalars(select(Job).where(Job.kind == KIND_INTAKE_EXTRACT)).all()
        assert len(jobs) == 1

    def test_the_whole_lifecycle_makes_one_row_and_one_job(
        self, client, session, signer, owned_number
    ):
        post(client, signer, call_payload(event="call_started", transcript=""))
        post(client, signer, call_payload(event="call_ended"))
        post(client, signer, call_payload(event="call_analyzed"))
        assert len(session.scalars(select(IntakeCall)).all()) == 1
        assert len(session.scalars(select(Job).where(Job.kind == KIND_INTAKE_EXTRACT)).all()) == 1

    def test_a_racing_insert_recovers_instead_of_500ing(
        self, session, account, owned_number
    ):
        """Two webhooks for one call, landing at once. Our SELECT sees nothing,
        then the INSERT collides.

        This is the path Retell's retries actually hit, and it has to recover
        silently: a 500 here makes the provider retry, which makes it collide
        again. `db.add` therefore belongs inside the savepoint, as in
        `jobs.enqueue` -- outside it, a colliding flush poisons the outer
        transaction and the recovery path itself raises.
        """
        from juicebox.intake import service

        org = account["org_id"]
        # The row another worker inserted a microsecond ago.
        session.add(IntakeCall(org_id=org, provider="retell", provider_call_id="call_race"))
        session.commit()

        event = RetellProvider(api_key=API_KEY).parse_event(
            call_payload(call_id="call_race")
        )

        seen = {"n": 0}
        real = service.find_call

        def stale_then_real(db, *, provider, provider_call_id):
            seen["n"] += 1
            if seen["n"] == 1:
                return None  # the stale read that loses the race
            return real(db, provider=provider, provider_call_id=provider_call_id)

        service.find_call = stale_then_real
        try:
            call, created = service.record_call_event(
                session, org_id=org, provider="retell", event=event
            )
        finally:
            service.find_call = real

        assert created is False
        assert call.provider_call_id == "call_race"
        assert call.transcript == TRANSCRIPT  # the losing event still applied
        assert len(session.scalars(select(IntakeCall)).all()) == 1

    def test_a_retried_webhook_is_a_no_op(self, client, session, signer, owned_number):
        """Providers retry. Three deliveries must not be three conversations."""
        for _ in range(3):
            assert post(client, signer, call_payload()).status_code == 200
        assert len(session.scalars(select(IntakeCall)).all()) == 1
        assert len(session.scalars(select(Job).where(Job.kind == KIND_INTAKE_EXTRACT)).all()) == 1

    def test_a_later_event_never_shortens_the_transcript(
        self, client, session, signer, owned_number
    ):
        post(client, signer, call_payload(event="call_ended"))
        post(client, signer, call_payload(event="call_analyzed", transcript="Agent: Hi."))
        call = session.scalars(select(IntakeCall)).one()
        assert call.transcript == TRANSCRIPT

    def test_an_unmapped_event_is_acknowledged_and_ignored(
        self, client, session, signer, owned_number
    ):
        """A provider adding an event type must not start failing this endpoint."""
        response = post(client, signer, call_payload(event="transcript_updated"))
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        assert session.scalars(select(IntakeCall)).all() == []

    def test_a_payload_with_no_call_id_is_rejected(self, client, signer, owned_number):
        assert post(client, signer, call_payload(call_id="")).status_code == 422

    def test_a_silent_call_queues_nothing(self, client, session, signer, owned_number):
        post(client, signer, call_payload(transcript=""))
        assert session.scalars(select(Job).where(Job.kind == KIND_INTAKE_EXTRACT)).all() == []

    def test_consent_is_recorded_when_the_agent_sets_it(
        self, client, session, signer, owned_number
    ):
        payload = call_payload(retell_llm_dynamic_variables={"consent_recording": True})
        post(client, signer, payload)
        assert session.scalars(select(IntakeCall)).one().consent_recording is True

    def test_consent_defaults_to_false(self, client, session, signer, owned_number):
        """A call with no recorded consent must never read as consented."""
        post(client, signer, call_payload())
        assert session.scalars(select(IntakeCall)).one().consent_recording is False

    def test_consent_is_read_from_post_call_analysis(
        self, client, session, signer, owned_number
    ):
        """The agent cannot set a dynamic variable mid-call, so the platform
        reads consent out of the transcript afterwards -- which arrives on
        call_analyzed, not call_ended."""
        payload = call_payload(
            event="call_analyzed",
            call_analysis={"custom_analysis_data": {"consent_recording": True}},
        )
        post(client, signer, payload)
        assert session.scalars(select(IntakeCall)).one().consent_recording is True

    def test_a_later_event_cannot_withdraw_consent(
        self, client, session, signer, owned_number
    ):
        """Latched, not overwritten. A follow-up event that simply omits the
        field must not silently un-consent a recording that was agreed to."""
        post(client, signer, call_payload(retell_llm_dynamic_variables={"consent_recording": True}))
        post(client, signer, call_payload(event="call_analyzed"))
        assert session.scalars(select(IntakeCall)).one().consent_recording is True


# --------------------------------------------------------------------------
# caller identification and extraction
# --------------------------------------------------------------------------


def _candidate(session, org_id, *, phones):
    person = Candidate(
        org_id=org_id, source="test", source_id="c1", full_name="Ada Okonkwo", phones=phones
    )
    session.add(person)
    session.flush()
    sync_candidate_phones(session, person)
    session.commit()
    return person


def _drain(session):
    from juicebox.worker import process_job

    jobs = session.scalars(select(Job).where(Job.kind == KIND_INTAKE_EXTRACT)).all()
    for job in jobs:
        job.status = "running"
        process_job(session, job)
    session.commit()
    return jobs


class TestExtraction:
    def test_a_known_caller_is_linked_by_number(
        self, client, session, signer, owned_number, account
    ):
        person = _candidate(session, account["org_id"], phones=[CANDIDATE_NUMBER])
        post(client, signer, call_payload())
        assert session.scalars(select(IntakeCall)).one().candidate_id == person.id

    def test_an_unknown_caller_is_not_linked(
        self, client, session, signer, owned_number, account
    ):
        _candidate(session, account["org_id"], phones=[CANDIDATE_NUMBER])
        post(client, signer, call_payload(from_number=STRANGER_NUMBER))
        assert session.scalars(select(IntakeCall)).one().candidate_id is None

    def test_a_number_belonging_to_another_org_does_not_match(
        self, client, session, signer, owned_number, account
    ):
        """CandidatePhone is org-scoped, and identification must respect that."""
        from juicebox.models import Org

        other = Org(name="Other Firm", slug="other-firm")
        session.add(other)
        session.flush()
        _candidate(session, other.id, phones=[CANDIDATE_NUMBER])
        post(client, signer, call_payload())
        assert session.scalars(select(IntakeCall)).one().candidate_id is None

    def test_the_worker_writes_a_preference_for_a_known_caller(
        self, client, session, signer, owned_number, account
    ):
        person = _candidate(session, account["org_id"], phones=[CANDIDATE_NUMBER])
        post(client, signer, call_payload())
        _drain(session)

        call = session.scalars(select(IntakeCall)).one()
        assert call.status == STATUS_EXTRACTED
        assert call.extractor == "heuristic"  # no model configured in tests
        pref = session.scalars(select(CandidatePreference)).one()
        assert pref.candidate_id == person.id
        assert pref.org_id == account["org_id"]
        assert pref.source_call_id == call.id
        assert pref.review_needed is True  # heuristic extraction always is

    def test_an_unidentified_caller_still_produces_a_record_but_no_preference(
        self, client, session, signer, owned_number
    ):
        post(client, signer, call_payload(from_number=STRANGER_NUMBER))
        _drain(session)

        call = session.scalars(select(IntakeCall)).one()
        assert call.status == STATUS_EXTRACTED
        assert call.extracted  # the conversation is not lost
        assert "caller not identified" in call.review_reason
        # Auto-creating a Candidate from a cold call would put strangers in the CRM.
        assert session.scalars(select(CandidatePreference)).all() == []

    def test_rerunning_the_job_does_not_overwrite(
        self, client, session, signer, owned_number, account
    ):
        _candidate(session, account["org_id"], phones=[CANDIDATE_NUMBER])
        post(client, signer, call_payload())
        _drain(session)
        pref = session.scalars(select(CandidatePreference)).one()
        pref.timeline = "immediate"
        session.commit()

        from juicebox.intake.service import extract_call

        call = session.scalars(select(IntakeCall)).one()
        assert extract_call(session, call) is None
        assert session.scalars(select(CandidatePreference)).one().timeline == "immediate"

    def test_a_confirmed_record_is_flagged_not_overwritten(
        self, client, session, signer, owned_number, account
    ):
        """A recruiter correcting a record and watching a re-run undo it is how
        people stop trusting the system."""
        from juicebox.intake.service import extract_call
        from juicebox.models import utcnow

        person = _candidate(session, account["org_id"], phones=[CANDIDATE_NUMBER])
        session.add(
            CandidatePreference(
                org_id=account["org_id"],
                candidate_id=person.id,
                timeline="3_6_months",
                confirmed_at=utcnow(),
                confirmed_by="user",
            )
        )
        session.commit()

        post(client, signer, call_payload())
        call = session.scalars(select(IntakeCall)).one()
        extract_call(session, call)
        session.commit()

        pref = session.scalars(select(CandidatePreference)).one()
        assert pref.timeline == "3_6_months"
        assert pref.review_needed is True
        assert "confirmed" in pref.review_reason

    def test_extraction_is_scoped_to_the_jobs_org(self, session, account):
        """Defence in depth: the id came from our own payload, but a mismatch
        between a job's org and its call's org means something is very wrong."""
        from juicebox.models import Org, utcnow
        from juicebox.worker import _handle_intake_extract

        other = Org(name="Other Firm", slug="other-firm")
        session.add(other)
        session.flush()
        call = IntakeCall(
            org_id=other.id,
            provider="retell",
            provider_call_id="call_x",
            transcript=TRANSCRIPT,
        )
        session.add(call)
        session.flush()
        job = Job(
            org_id=account["org_id"],
            kind=KIND_INTAKE_EXTRACT,
            payload={"intake_call_id": call.id},
            idempotency_key="k",
            status="running",
        )
        session.add(job)
        session.flush()

        # process_job discards the handler's return, so call the handler.
        assert _handle_intake_extract(session, job, utcnow()) == {"skipped": "org_mismatch"}
        assert call.status != STATUS_EXTRACTED
        assert session.scalars(select(CandidatePreference)).all() == []


# --------------------------------------------------------------------------
# the mid-call gate
# --------------------------------------------------------------------------


class TestCoverageEndpoint:
    def _function_call(self, transcript, call_id="call_abc123"):
        return {
            "name": "check_coverage",
            "call": {"call_id": call_id, "transcript": transcript, "agent_id": "agent_1"},
            "args": {},
        }

    def test_an_empty_transcript_asks_the_gate_first(self, client, signer):
        response = post(
            client, signer, self._function_call(""), path="/v1/intake/coverage"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["wrap_up"] is False
        assert body["still_needed"]
        assert body["optional_topics"] == []  # do not distract from the gate
        assert body["required_outstanding"] > 0

    def test_it_requires_a_signature(self, client):
        response = client.post("/v1/intake/coverage", json=self._function_call(""))
        assert response.status_code == 401

    def test_a_partial_transcript_still_owes_the_gate(self, client, signer):
        response = post(
            client, signer, self._function_call(TRANSCRIPT), path="/v1/intake/coverage"
        )
        body = response.json()
        assert body["wrap_up"] is False
        assert "one at a time" in body["guidance"]

    def test_it_returns_questions_not_people(self, client, session, signer, owned_number, account):
        """Reachable by anyone with a valid signature, so it must not disclose a
        candidate's record — only which topics remain."""
        _candidate(session, account["org_id"], phones=[CANDIDATE_NUMBER])
        post(client, signer, call_payload())
        response = post(
            client, signer, self._function_call(TRANSCRIPT), path="/v1/intake/coverage"
        )
        rendered = json.dumps(response.json())
        assert "Ada" not in rendered
        assert CANDIDATE_NUMBER not in rendered

    def test_a_covered_conversation_is_told_to_wrap_up(self, client, signer, monkeypatch):
        """The two-to-three-minute release, driven by the record rather than a clock."""
        from juicebox.intake import service

        def _complete(transcript, *, known="", fast=True):
            from juicebox.intake.extract import ExtractionResult
            from juicebox.intake.schema import coverage

            record = _filled_required()
            return ExtractionResult(
                record=record, extractor="llm", coverage=coverage(record)
            )

        monkeypatch.setattr(service, "extract_intake", _complete)
        response = post(
            client, signer, self._function_call(TRANSCRIPT), path="/v1/intake/coverage"
        )
        body = response.json()
        assert body["wrap_up"] is True
        # Nothing is *owed*. Enrichment moves to optional_topics rather than
        # keeping a finished call alive.
        assert body["still_needed"] == []
        assert body["optional_topics"]
        assert body["optional_topics"][0]["topic"] == "highlights"
        assert "may close" in body["guidance"]


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
