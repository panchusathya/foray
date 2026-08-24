"""Messaging intake: cadence, resumption, opt-out, and idempotency.

The voice suite defends what gets *collected*. This one defends what gets
*sent*, because that is the half messaging adds and the half that can do harm.

**Sending one message too many is the expensive failure.** A call that runs long
is disliked; a thread that keeps asking gets the number blocked, and every
candidate after that one silently receives nothing. So the rules the
conversation enforces -- one question per message, never two outbound in a row,
two attempts at any field, a budget on enrichment -- are tested as invariants
rather than as behaviour that happens to hold today.

**A refusal is still an answer, and an unanswered question is still not a
refusal.** A field nobody would answer gets abandoned and flagged, never marked
declined. Recording a refusal that did not happen would put an invented answer
into a record matching reads.

**The whole thing runs with no API key.** Every test here does, because the
suite disables the model globally -- the canned question in ``schema.py`` is a
real question, so the offline path is a flatter conversation and not a broken
one.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from intake_fixtures import COMPLETE_PAYLOAD
from sqlalchemy import select

from juicebox.intake.schema import REQUIRED_KEYS, coverage, empty_record
from juicebox.jobs import KIND_INTAKE_TURN, KIND_JOBS_MATCH_ONE, enqueue
from juicebox.llm.client import FakeTransport, LLMClient, set_llm
from juicebox.messaging import prompts
from juicebox.messaging.channels import (
    FAKE,
    LINQ,
    WEB,
    ChannelError,
    FakeChannel,
    get_channel,
    set_channel,
)
from juicebox.messaging.conversation import (
    ACTION_BLOCKED,
    ACTION_COMPLETED,
    ACTION_GREETED,
    ACTION_HELD,
    ACTION_STOPPED,
    MAX_ASKS_PER_FIELD,
    MAX_ENRICHMENT_QUESTIONS,
    _compose,
    _next_question,
    run_turn,
)
from juicebox.messaging.identity import (
    CONTACT_EMAIL,
    CONTACT_NAME,
    UNATTRIBUTED_REASON,
    find_email,
    find_name,
)
from juicebox.messaging.linq import sign_webhook
from juicebox.messaging.records import (
    merge_record,
    persist_preference,
    record_from_preference,
)
from juicebox.messaging.suppression import (
    address_has_opted_out,
    handle_stop,
    is_blocked,
    looks_like_stop,
)
from juicebox.messaging.threads import (
    STATUS_COMPLETE,
    STATUS_STOPPED,
    open_thread,
    record_inbound,
    transcript_of,
)
from juicebox.models import (
    Candidate,
    CandidatePreference,
    IntakeMessage,
    IntakeNumber,
    IntakeThread,
    Job,
    utcnow,
)
from juicebox.outreach.suppressions import is_suppressed, suppress
from juicebox.repository import upsert_candidates
from juicebox.routers.messaging import PURPOSE_THREAD, PURPOSE_WIDGET
from juicebox.security import signed_intake_token

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def thread(session, account, fake_channel):
    return open_thread(
        session, org_id=account["org_id"], channel=WEB, consent_messaging=True
    )


@pytest.fixture
def identified(session, account, fake_channel):
    """A thread past the identity questions, so tests can reach the gate."""
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=WEB,
        consent_messaging=True,
        contact_name="Ada Lovelace",
        contact_email="ada@example.com",
    )


@pytest.fixture
def widget_token(account):
    org_id = account["org_id"]
    return f"{org_id}.{signed_intake_token(PURPOSE_WIDGET, org_id)}"


def _say(session, thread, body, *, native_id=""):
    """One inbound message plus the turn it triggers."""
    record_inbound(session, thread=thread, body=body, provider_native_id=native_id)
    return run_turn(session, thread=thread)


def _filled_required() -> dict:
    """A record with every required field answered and no enrichment."""
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
            "comp_currency": "USD",
            "work_authorization": "green card",
            "active_processes": [{"company": "none", "stage": "none"}],
            "ok_to_share_with_clients": True,
        }
    )
    return record


# --------------------------------------------------------------------------
# the channel seam
# --------------------------------------------------------------------------


class TestChannels:
    def test_an_unknown_channel_is_a_hard_error(self):
        """Never a silent default: falling back would drop a message and
        report success."""
        with pytest.raises(ChannelError):
            get_channel("carrier-pigeon")

    def test_web_needs_no_consent_because_they_opened_it(self):
        assert get_channel(WEB).requires_consent() is False

    def test_a_channel_that_needs_consent_refuses_without_it(self, session, account):
        channel = FakeChannel(consent=True)
        set_channel(FAKE, channel)
        thread = open_thread(
            session, org_id=account["org_id"], channel=FAKE, consent_messaging=False
        )
        run_turn(session, thread=thread)

        assert channel.sent == []
        failed = session.scalars(
            select(IntakeMessage).where(IntakeMessage.thread_id == thread.id)
        ).all()
        assert failed and all(m.status == "failed" for m in failed)
        assert "consent" in failed[0].error

    def test_a_delivery_failure_is_recorded_not_swallowed(self, session, thread, fake_channel):
        fake_channel.fail_next = "carrier rejected"
        run_turn(session, thread=thread)

        rows = session.scalars(
            select(IntakeMessage)
            .where(IntakeMessage.thread_id == thread.id)
            .order_by(IntakeMessage.seq)
        ).all()
        assert rows[0].status == "failed"
        assert rows[0].error == "carrier rejected"
        # A failed send is not an ask: only the question that landed counts,
        # or the thread would believe it had asked something nobody saw.
        assert thread.outbound_count == 1
        assert rows[1].status == "sent"


# --------------------------------------------------------------------------
# the merge
# --------------------------------------------------------------------------


class TestMerge:
    def test_a_filled_field_survives_a_quieter_later_pass(self):
        """The reason the gate cannot oscillate.

        Re-extraction runs over a growing transcript, and a later pass can come
        back emptier. If that cleared the record, the gate would reopen and the
        thread would re-ask a question already answered.
        """
        current = merge_record(empty_record(), {"timeline": "under_1_month"})
        later = merge_record(current, {"timeline": "", "employment_status": "student"})
        assert later["timeline"] == "under_1_month"
        assert later["employment_status"] == "student"

    def test_a_correction_overwrites(self):
        current = merge_record(empty_record(), {"target_base": 240000.0})
        corrected = merge_record(current, {"target_base": 260000.0})
        assert corrected["target_base"] == 260000.0

    def test_false_is_an_answer_and_replaces_nothing_silently(self):
        current = merge_record(empty_record(), {"spec": {"remote_ok": False}})
        assert current["spec"]["remote_ok"] is False
        # None means never asked, so it must not overwrite a real False.
        after = merge_record(current, {"spec": {"remote_ok": None}})
        assert after["spec"]["remote_ok"] is False

    def test_declines_accumulate_and_are_never_withdrawn(self):
        current = merge_record(empty_record(), {"declined": ["compensation"]})
        after = merge_record(current, {"declined": ["work_authorization"]})
        assert set(after["declined"]) == {"compensation", "work_authorization"}

    def test_a_stored_preference_round_trips_through_the_record_shape(
        self, session, account
    ):
        """A caller who later texts must not lose what the call captured."""
        ids = upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[{"source": "test", "source_id": "c1", "full_name": "Ada Lovelace"}],
        ).ids
        pref = CandidatePreference(
            org_id=account["org_id"],
            candidate_id=ids[0],
            spec={"job_titles": ["machine learning engineer"]},
            timeline="under_1_month",
            declined=["compensation"],
        )
        session.add(pref)
        session.flush()

        record = record_from_preference(pref)
        assert record["spec"]["job_titles"] == ["machine learning engineer"]
        assert record["timeline"] == "under_1_month"
        assert record["declined"] == ["compensation"]


# --------------------------------------------------------------------------
# cadence: the messaging-specific design
# --------------------------------------------------------------------------


class TestCadence:
    def test_the_opening_turn_greets_and_asks_exactly_one_thing(
        self, session, thread, fake_channel
    ):
        result = run_turn(session, thread=thread)
        assert result.action == ACTION_GREETED
        assert len(fake_channel.bodies) == 2  # greeting, then one question
        assert fake_channel.bodies[1].count("?") == 1
        assert result.asked_field == CONTACT_NAME

    def test_it_never_sends_twice_in_a_row(self, session, thread, fake_channel):
        run_turn(session, thread=thread)
        before = len(fake_channel.sent)

        held = run_turn(session, thread=thread)

        assert held.action == ACTION_HELD
        assert len(fake_channel.sent) == before

    def test_one_question_per_inbound_message(self, session, identified, fake_channel):
        run_turn(session, thread=identified)
        sent_after_greeting = len(fake_channel.sent)

        _say(session, identified, "senior machine learning engineer roles")

        assert len(fake_channel.sent) == sent_after_greeting + 1

    def test_a_field_is_asked_at_most_twice_then_abandoned(self, session, identified):
        """And abandoned is not declined -- nobody refused it."""
        identified.record = _filled_required()
        identified.asked_counts = {key: MAX_ASKS_PER_FIELD for key in REQUIRED_KEYS}
        # Reopen one required field so the gate is genuinely open.
        identified.record["timeline"] = ""
        session.flush()

        assert _next_question(identified.record, identified.asked_counts) is None
        assert "timeline" not in (identified.record.get("declined") or [])

    def test_required_is_exhausted_before_enrichment_is_touched(self, session):
        record = empty_record()
        question = _next_question(record, {})
        assert question is not None
        assert question["tier"] == "required"

    def test_enrichment_is_budgeted(self):
        """A call asks all fourteen; a text cannot."""
        record = _filled_required()
        assert _next_question(record, {})["tier"] == "enrichment"

        spent = {}
        state = coverage(record)
        for key in state.enrichment_missing[:MAX_ENRICHMENT_QUESTIONS]:
            spent[key] = 1
        assert _next_question(record, spent) is None

    def test_a_declined_field_is_never_asked_again(self):
        record = _filled_required()
        record["target_base"] = None
        record["comp_currency"] = ""
        assert "compensation" in coverage(record).required_missing

        record["declined"] = ["compensation"]
        question = _next_question(record, {})
        assert question is None or question["field"] != "compensation"


# --------------------------------------------------------------------------
# the transcript handed to the shared extractor
# --------------------------------------------------------------------------


class TestTranscript:
    def test_it_is_framed_the_way_a_call_transcript_is(
        self, session, identified, fake_channel
    ):
        run_turn(session, thread=identified)
        _say(session, identified, "senior ML engineer")

        text = transcript_of(session, identified)
        assert "Agent: " in text
        assert "Candidate: senior ML engineer" in text

    def test_the_heuristic_never_reads_our_own_words(
        self, session, thread, fake_channel
    ):
        """The bug this exists to stop.

        The heuristic extractor is regexes over the whole text with no notion
        of who is speaking. Given the agent's greeting it read a job title of
        "take few details so recruiter can match you to right" and a seniority
        of "mid" out of a conversation where the candidate had said only their
        name -- closing two required gates on words we wrote ourselves.
        """
        run_turn(session, thread=thread)
        _say(session, thread, "Ada Lovelace")

        assert "Agent:" in transcript_of(session, thread)
        assert "Agent:" not in transcript_of(session, thread, candidate_only=True)
        # Nothing invented: nobody has said anything about a role yet.
        assert thread.record["spec"]["job_titles"] == []
        assert thread.record["spec"]["seniority"] == []
        assert "spec.job_titles" in coverage(thread.record).required_missing

    def test_an_undelivered_message_is_not_part_of_the_conversation(
        self, session, thread, fake_channel
    ):
        """They never saw it, so an answer cannot be a response to it."""
        fake_channel.fail_next = "carrier rejected"
        result = run_turn(session, thread=thread)

        text = transcript_of(session, thread)
        assert "intake assistant" not in text  # the greeting never arrived
        assert result.asked_field in text or text.startswith("Agent: ")


# --------------------------------------------------------------------------
# identity, which lives off the spec on purpose
# --------------------------------------------------------------------------


class TestIdentity:
    def test_an_address_is_read_from_any_message(self):
        assert find_email("sure, ada@example.com works") == "ada@example.com"

    def test_a_name_is_only_read_from_an_answer_that_looks_like_one(self):
        assert find_name("I'm Ada Lovelace") == "Ada Lovelace"
        assert find_name("I want staff backend roles in New York") == ""
        assert find_name("agent47@example.com") == ""

    def test_identity_is_asked_before_the_gate(self, session, thread, fake_channel):
        first = run_turn(session, thread=thread)
        assert first.asked_field == CONTACT_NAME

        second = _say(session, thread, "Ada Lovelace")
        assert second.asked_field == CONTACT_EMAIL

    def test_contact_details_never_enter_the_spec(self, session, thread, fake_channel):
        run_turn(session, thread=thread)
        _say(session, thread, "Ada Lovelace")
        _say(session, thread, "ada@example.com")

        assert thread.contact_name == "Ada Lovelace"
        assert thread.contact_email == "ada@example.com"
        assert "contact_name" not in thread.record["spec"]
        assert "contact_email" not in thread.record["spec"]

    def test_no_candidate_row_is_created_before_the_conversation_ends(
        self, session, thread, fake_channel, account
    ):
        """A public endpoint that created one per chat is a CRM spam vector."""
        run_turn(session, thread=thread)
        _say(session, thread, "Ada Lovelace")
        _say(session, thread, "ada@example.com")

        assert thread.candidate_id is None
        assert session.scalars(select(Candidate)).all() == []

    def test_an_existing_owner_of_the_address_wins(
        self, session, identified, fake_channel, account
    ):
        """Splitting somebody's history across two rows is worse than no row."""
        existing = upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[
                {
                    "source": "test",
                    "source_id": "ada",
                    "full_name": "Ada Lovelace",
                    "emails": ["ada@example.com"],
                }
            ],
        ).ids[0]

        from juicebox.messaging.identity import link_candidate

        assert link_candidate(session, thread=identified) == existing
        session.refresh(session.get(Candidate, existing))
        assert session.get(Candidate, existing).crm_tracked is True


# --------------------------------------------------------------------------
# opt-out and do-not-contact
# --------------------------------------------------------------------------


class TestSuppression:
    def test_the_whole_message_has_to_be_the_keyword(self):
        assert looks_like_stop("STOP") is True
        assert looks_like_stop(" stop. ") is True
        assert looks_like_stop("stop me if you have heard this one") is False

    def test_a_do_not_contact_candidate_receives_nothing(
        self, session, account, fake_channel
    ):
        candidate_id = upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[
                {
                    "source": "test",
                    "source_id": "ada",
                    "full_name": "Ada Lovelace",
                    "emails": ["ada@example.com"],
                }
            ],
        ).ids[0]
        suppress(session, org_id=account["org_id"], email="ada@example.com")

        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=WEB,
            candidate_id=candidate_id,
            consent_messaging=True,
        )
        result = run_turn(session, thread=thread)

        assert result.action == ACTION_BLOCKED
        assert fake_channel.sent == []
        assert thread.status == STATUS_STOPPED

    def test_stop_closes_the_thread_and_suppresses_the_address(
        self, session, identified, fake_channel
    ):
        run_turn(session, thread=identified)
        result = _say(session, identified, "STOP")

        assert result.action == ACTION_STOPPED
        assert identified.status == STATUS_STOPPED
        # Exactly one message after the opt-out: the confirmation a carrier
        # expects, and nothing else, ever.
        assert fake_channel.bodies[-1] == prompts.STOP_ACKNOWLEDGEMENT

        from juicebox.outreach.suppressions import is_suppressed

        assert is_suppressed(session, org_id=identified.org_id, email="ada@example.com")

    def test_a_stopped_thread_stays_silent_afterwards(
        self, session, identified, fake_channel
    ):
        run_turn(session, thread=identified)
        _say(session, identified, "STOP")
        before = len(fake_channel.sent)

        result = _say(session, identified, "actually wait")

        assert result.action == ACTION_BLOCKED
        assert len(fake_channel.sent) == before

    def test_an_opt_out_binds_to_the_sender_not_the_conversation(
        self, session, account, fake_channel
    ):
        """A STOP that only stopped one thread would be no opt-out at all."""
        first = open_thread(
            session,
            org_id=account["org_id"],
            channel=WEB,
            from_address="+14155550123",
            consent_messaging=True,
        )
        run_turn(session, thread=first)
        _say(session, first, "STOP")

        second = open_thread(
            session,
            org_id=account["org_id"],
            channel=WEB,
            from_address="+14155550123",
            consent_messaging=True,
        )
        before = len(fake_channel.sent)
        result = run_turn(session, thread=second)

        assert result.action == ACTION_BLOCKED
        assert len(fake_channel.sent) == before


# --------------------------------------------------------------------------
# composing, with and without a model
# --------------------------------------------------------------------------


class TestCompose:
    def test_every_intake_field_has_a_question(self):
        """The table is a copy of the field set and has to stay in step.

        ``IntakeField.asks`` is a topic line for a voice agent's checklist --
        "Which roles they are actually going after." Sending that verbatim asks
        somebody to describe themselves in the third person, so messaging keeps
        its own wording, and this is what stops the two drifting.
        """
        from juicebox.intake.schema import FIELDS

        assert {f.key for f in FIELDS} == set(prompts.QUESTIONS)

    def test_a_written_question_is_addressed_to_the_candidate(self):
        """Second person, and an actual question.

        Exactly one question mark, though not necessarily at the end -- "Base
        or total is fine." after the question is a clarifier, not a second ask,
        and valid_question is what draws that line.
        """
        for key, question in prompts.QUESTIONS.items():
            assert "?" in question, key
            assert prompts.valid_question(question), key
            # A topic line reads "Which roles they are going after"; a question
            # to somebody never says "they".
            assert " they " not in f" {question.lower()} ", key

    def test_an_unknown_field_falls_back_to_the_schema_line(self):
        assert prompts.question_for("spec.brand_new", "Topic line.") == "Topic line."

    def test_with_no_api_key_the_canned_question_is_sent(self):
        """Not a degraded mode: schema.asks is a real, complete question."""
        assert _compose(asks="What roles?", topic="", last_inbound="hi", known="") == "What roles?"

    def test_the_model_rewrite_is_used_when_it_is_valid(self):
        set_llm(
            LLMClient(
                api_key="test-key",
                transport=FakeTransport(default={"message": "Got it. What roles?"}),
                enabled=True,
            )
        )
        try:
            assert _compose(asks="What roles?", topic="", last_inbound="hi", known="") == (
                "Got it. What roles?"
            )
        finally:
            set_llm(None)

    def test_a_second_question_is_rejected_rather_than_sent(self):
        """An instruction not to ask two things is a hope; this is a rule."""
        set_llm(
            LLMClient(
                api_key="test-key",
                transport=FakeTransport(
                    default={"message": "What roles? And what comp?"}
                ),
                enabled=True,
            )
        )
        try:
            assert _compose(asks="What roles?", topic="", last_inbound="hi", known="") == "What roles?"
        finally:
            set_llm(None)

    def test_an_overlong_rewrite_is_rejected(self):
        assert prompts.valid_question("x" * (prompts.MAX_QUESTION_CHARS + 1)) is False
        assert prompts.valid_question("Two paragraphs\n\nsecond one") is False


# --------------------------------------------------------------------------
# the public chat API
# --------------------------------------------------------------------------


class TestChatApi:
    def test_a_recruiter_can_mint_the_widget_token(self, client, account):
        response = client.get("/v1/intake/widget-token", headers=account["headers"])
        assert response.status_code == 200
        assert response.json()["token"].startswith(account["org_id"] + ".")

    def test_an_unsigned_or_forged_token_is_refused(self, client, account):
        assert client.post("/v1/intake/chat/sessions", json={"token": "nope"}).status_code == 401
        forged = f"{account['org_id']}.deadbeef"
        assert (
            client.post("/v1/intake/chat/sessions", json={"token": forged}).status_code == 401
        )

    def test_a_thread_token_cannot_be_replayed_as_a_widget_token(self, client, account):
        """Purpose is inside the signature for exactly this."""
        org_id = account["org_id"]
        thread_scoped = f"{org_id}.{signed_intake_token(PURPOSE_THREAD, org_id)}"
        response = client.post("/v1/intake/chat/sessions", json={"token": thread_scoped})
        assert response.status_code == 401

    def test_a_stranger_can_open_a_conversation(self, client, widget_token):
        response = client.post("/v1/intake/chat/sessions", json={"token": widget_token})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["thread_token"]
        assert len(body["messages"]) == 2
        assert body["messages"][0]["direction"] == "outbound"

    def test_the_org_thread_creation_cap_refuses_excess_sessions(
        self, client, widget_token, monkeypatch
    ):
        from juicebox.routers import messaging as messaging_router

        monkeypatch.setattr(messaging_router, "MAX_NEW_THREADS_PER_ORG_PER_HOUR", 2)

        assert (
            client.post("/v1/intake/chat/sessions", json={"token": widget_token}).status_code
            == 201
        )
        assert (
            client.post("/v1/intake/chat/sessions", json={"token": widget_token}).status_code
            == 201
        )
        blocked = client.post("/v1/intake/chat/sessions", json={"token": widget_token})
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == "Too many new conversations for this organization"

    def test_a_duplicate_client_message_id_is_a_no_op(self, client, widget_token, session):
        started = client.post(
            "/v1/intake/chat/sessions", json={"token": widget_token}
        ).json()
        payload = {
            "token": started["thread_token"],
            "body": "Ada Lovelace",
            "client_message_id": "m-1",
        }
        first = client.post(
            f"/v1/intake/chat/sessions/{started['thread_id']}/messages", json=payload
        ).json()
        second = client.post(
            f"/v1/intake/chat/sessions/{started['thread_id']}/messages", json=payload
        ).json()

        assert second["action"] == "duplicate"
        assert len(second["messages"]) == len(first["messages"])

    def test_another_thread_token_cannot_read_this_conversation(
        self, client, widget_token
    ):
        started = client.post(
            "/v1/intake/chat/sessions", json={"token": widget_token}
        ).json()
        other = signed_intake_token(PURPOSE_THREAD, "ithread_somebody_else")
        response = client.get(
            f"/v1/intake/chat/sessions/{started['thread_id']}", params={"token": other}
        )
        assert response.status_code == 401

    def test_a_dormant_thread_resumes_where_it_stopped(self, client, widget_token):
        """Nothing already answered is asked again."""
        started = client.post(
            "/v1/intake/chat/sessions", json={"token": widget_token}
        ).json()
        thread_id, token = started["thread_id"], started["thread_token"]
        client.post(
            f"/v1/intake/chat/sessions/{thread_id}/messages",
            json={"token": token, "body": "Ada Lovelace", "client_message_id": "m-1"},
        )

        resumed = client.get(
            f"/v1/intake/chat/sessions/{thread_id}", params={"token": token}
        ).json()
        assert resumed["status"] == "active"
        asked = [m["asked_field"] for m in resumed["messages"] if m["asked_field"]]
        # The name was answered, so it is never asked a second time.
        assert asked.count(CONTACT_NAME) == 1
        assert asked[-1] == CONTACT_EMAIL

    def test_a_finished_conversation_writes_a_candidate_preference(
        self, client, widget_token, session, account
    ):
        """The definition of done: same record shape a call produces."""
        set_llm(
            LLMClient(
                api_key="test-key",
                # Every model call in the turn gets this. Extraction reads it as
                # a full record; compose finds no "message" key and falls back
                # to the canned question, which is the behaviour we want anyway.
                transport=FakeTransport(default=COMPLETE_PAYLOAD),
                enabled=True,
            )
        )
        try:
            started = client.post(
                "/v1/intake/chat/sessions", json={"token": widget_token}
            ).json()
            thread_id, token = started["thread_id"], started["thread_token"]

            answers = ["Ada Lovelace", "ada@example.com", "senior ML engineer in SF"]
            last = None
            for index, answer in enumerate(answers):
                last = client.post(
                    f"/v1/intake/chat/sessions/{thread_id}/messages",
                    json={
                        "token": token,
                        "body": answer,
                        "client_message_id": f"m-{index}",
                    },
                ).json()
        finally:
            set_llm(None)

        assert last["required_complete"] is True

        thread = session.get(IntakeThread, thread_id)
        session.refresh(thread)
        pref = session.scalar(
            select(CandidatePreference).where(
                CandidatePreference.source_thread_id == thread_id
            )
        )
        assert pref is not None
        # Normalised, not verbatim. The fixture says "Sr. ML Engineer"; what
        # lands is the token a parsed job description carries, which is the
        # whole reason intake reuses ParsedQuery's vocabulary.
        assert COMPLETE_PAYLOAD["spec"]["job_titles"] == ["Sr. ML Engineer"]
        assert pref.spec["job_titles"] == ["machine learning engineer"]
        assert pref.spec["seniority"] == ["senior"]
        # Exactly one provenance link is set: this row was last written by a
        # thread, not a call.
        assert pref.source_call_id is None
        candidate = session.get(Candidate, pref.candidate_id)
        assert candidate.full_name == "Ada Lovelace"
        assert candidate.crm_tracked is True

        listed = client.get(
            "/v1/candidates",
            headers=account["headers"],
            params={"scope": "crm"},
        ).json()
        assert any(row["id"] == candidate.id for row in listed)

        preference = client.get(
            f"/v1/intake/preferences/{candidate.id}",
            headers=account["headers"],
        ).json()
        assert preference["source_thread_id"] == thread_id
        assert preference["source_call_id"] is None

        detail = client.get(
            f"/v1/intake/threads/{thread_id}",
            headers=account["headers"],
        )
        assert detail.status_code == 200, detail.text
        assert len(detail.json()["messages"]) >= 2

        timeline = client.get(
            f"/v1/candidates/{candidate.id}/timeline",
            headers=account["headers"],
        ).json()
        assert any(row["thread_id"] == thread_id for row in timeline["intake_threads"])
        assert timeline["intake_threads"][0]["messages"]

    def test_an_unanswerable_conversation_still_terminates(
        self, client, widget_token, session
    ):
        """The heuristic extractor cannot fill every required field, so this is
        the thread that would loop forever without the ask cap. It must end
        with a partial record rather than keep asking."""
        started = client.post(
            "/v1/intake/chat/sessions", json={"token": widget_token}
        ).json()
        thread_id, token = started["thread_id"], started["thread_token"]

        last = None
        for index in range(40):
            last = client.post(
                f"/v1/intake/chat/sessions/{thread_id}/messages",
                json={
                    "token": token,
                    "body": "not sure",
                    "client_message_id": f"m-{index}",
                },
            ).json()
            if last["status"] == STATUS_COMPLETE:
                break

        assert last["status"] == STATUS_COMPLETE
        assert last["required_complete"] is False
        # Nothing silently absent: the record says what it never got.
        assert last["fields_missing"]

        thread = session.get(IntakeThread, thread_id)
        session.refresh(thread)
        assert thread.review_needed is True
        assert thread.record["declined"] == []


# --------------------------------------------------------------------------
# the queued path SMS will use
# --------------------------------------------------------------------------


class TestPreferenceHandoff:
    """Where a thread write meets the row the voice path also writes.

    Both paths upsert one ``CandidatePreference`` per candidate, and neither
    conflicted textually when the branches merged -- so these are the cases a
    clean merge hid.
    """

    def _candidate(self, session, account):
        upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[
                {
                    "source": "handoff",
                    "source_id": "h1",
                    "full_name": "Ada Handoff",
                    "emails": ["ada-handoff@example.com"],
                    "current_title": "ML Engineer",
                }
            ],
        )
        session.commit()
        return session.scalar(select(Candidate).where(Candidate.full_name == "Ada Handoff"))

    def _thread_saying(self, session, account, **record):
        thread = open_thread(
            session, org_id=account["org_id"], channel=WEB, consent_messaging=True
        )
        spec = record.pop("spec", {})
        thread.record = {
            **thread.record,
            **record,
            "spec": {**thread.record["spec"], **spec},
        }
        session.commit()
        return thread

    def test_a_thread_write_enqueues_a_rescore(self, session, account, fake_channel):
        """Otherwise a chat intake writes a preference nothing ever scores."""
        candidate = self._candidate(session, account)
        thread = self._thread_saying(
            session, account, spec={"job_titles": ["machine learning engineer"]}
        )
        persist_preference(session, thread=thread, candidate_id=candidate.id)
        session.commit()

        queued = session.scalars(
            select(Job).where(
                Job.kind == KIND_JOBS_MATCH_ONE, Job.org_id == account["org_id"]
            )
        ).all()
        assert len(queued) == 1
        assert queued[0].payload["candidate_id"] == candidate.id

    def test_a_thread_may_change_a_confirmed_record_but_not_keep_the_confirmation(
        self, session, account, fake_channel
    ):
        """The candidate's own words outrank a recruiter's transcription.

        What must not survive is the flag: a row still claiming ``confirmed_at``
        over content the confirmer never saw is worse than either overwriting or
        refusing, because everything downstream trusts it.
        """
        candidate = self._candidate(session, account)
        pref = CandidatePreference(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            spec={"job_titles": ["staff platform engineer"]},
            timeline="3_6_months",
            confirmed_at=utcnow(),
            confirmed_by="a-real-human",
        )
        session.add(pref)
        session.commit()

        thread = self._thread_saying(
            session,
            account,
            spec={"job_titles": ["senior backend engineer"]},
            timeline="immediate",
        )
        persist_preference(session, thread=thread, candidate_id=candidate.id)
        session.commit()
        session.refresh(pref)

        assert pref.spec["job_titles"] == ["senior backend engineer"]
        assert pref.timeline == "immediate"
        assert pref.confirmed_at is None
        assert pref.confirmed_by == ""
        assert pref.review_needed is True
        assert thread.id in pref.review_reason

    def test_an_unconfirmed_record_keeps_its_blank_confirmation(
        self, session, account, fake_channel
    ):
        """The invalidation path must not invent a review reason for everyone."""
        candidate = self._candidate(session, account)
        thread = self._thread_saying(
            session, account, spec={"job_titles": ["machine learning engineer"]}
        )
        pref = persist_preference(session, thread=thread, candidate_id=candidate.id)
        session.commit()

        assert pref.confirmed_at is None
        assert "changed a confirmed record" not in (pref.review_reason or "")


class TestThreadsOnTheReviewQueue:
    """The review queue reads calls; without this it never sees a thread.

    ``IntakeThread.review_needed`` defaults to true, so a queue that only
    knows about calls flags every chat and shows none of them.
    """

    def _other_headers(self, client):
        other = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival Co",
                "email": "rival-threads@example.com",
                "password": "rival-password-123",
            },
        ).json()
        return {"Authorization": f"Bearer {other['access_token']}"}

    def test_a_flagged_thread_reaches_the_queue(
        self, client, session, account, fake_channel
    ):
        thread = open_thread(
            session, org_id=account["org_id"], channel=WEB, consent_messaging=True
        )
        thread.review_needed = True
        thread.review_reason = "gate never closed"
        session.commit()

        rows = client.get(
            "/v1/intake/threads?review_needed=true", headers=account["headers"]
        ).json()
        assert [row["id"] for row in rows] == [thread.id]
        assert rows[0]["channel"] == WEB
        assert rows[0]["review_reason"] == "gate never closed"

        counted = client.get(
            "/v1/intake/threads/count?review_needed=true", headers=account["headers"]
        ).json()
        assert counted["count"] == 1

    def test_another_org_cannot_read_our_threads(
        self, client, session, account, fake_channel
    ):
        open_thread(session, org_id=account["org_id"], channel=WEB, consent_messaging=True)
        session.commit()
        rival = self._other_headers(client)
        assert client.get("/v1/intake/threads", headers=rival).json() == []
        assert client.get("/v1/intake/threads/count", headers=rival).json()["count"] == 0

    def test_the_queue_can_ask_for_unattributed_threads_only(
        self, client, session, account, fake_channel
    ):
        """Who was this? is the human action the queue exists for."""
        upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[{"source": "q", "source_id": "q1", "full_name": "Known Person"}],
        )
        session.commit()
        known = session.scalar(select(Candidate).where(Candidate.full_name == "Known Person"))

        attributed = open_thread(
            session, org_id=account["org_id"], channel=WEB, consent_messaging=True
        )
        attributed.candidate_id = known.id
        stranger = open_thread(
            session, org_id=account["org_id"], channel=WEB, consent_messaging=True
        )
        session.commit()

        rows = client.get(
            "/v1/intake/threads?unattributed=true", headers=account["headers"]
        ).json()
        ids = {row["id"] for row in rows}
        assert stranger.id in ids
        assert attributed.id not in ids


class TestPhoneOptOut:
    """A texter's STOP has to be durable outside messaging, now that it can be.

    Before ``Suppression`` carried a phone column a stopped thread was the only
    record, so nothing else in the stack could see the opt-out.
    """

    def test_stop_suppresses_the_number_it_came_from(self, session, account, fake_channel):
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=WEB,
            consent_messaging=True,
            from_address="+14155550123",
        )
        session.commit()
        handle_stop(session, thread=thread)
        session.commit()

        assert is_suppressed(session, org_id=account["org_id"], phone="+14155550123")
        # And it reads back, which is the half that makes writing it worth it.
        fresh = open_thread(
            session,
            org_id=account["org_id"],
            channel=WEB,
            consent_messaging=True,
            from_address="+14155550123",
        )
        session.commit()
        assert is_blocked(session, thread=fresh)

    def test_a_number_suppressed_elsewhere_blocks_a_new_thread(
        self, session, account, fake_channel
    ):
        """A recruiter or the outreach stack can opt a number out too."""
        suppress(session, org_id=account["org_id"], phone="+14155550999", reason="unsubscribe")
        session.commit()
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=WEB,
            consent_messaging=True,
            from_address="+14155550999",
        )
        session.commit()
        assert is_blocked(session, thread=thread)

    def test_an_unparseable_number_still_leaves_the_stopped_thread(
        self, session, account, fake_channel
    ):
        """lookup_key refuses to guess a country, so there is no row to write."""
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=WEB,
            consent_messaging=True,
            from_address="12345",
        )
        session.commit()
        handle_stop(session, thread=thread)
        session.commit()

        assert not is_suppressed(session, org_id=account["org_id"], phone="12345")
        # The stopgap is what still catches them.
        assert address_has_opted_out(session, channel=WEB, from_address="12345")


class TestThreadAttribution:
    """The human action the review queue exists for."""

    def _person(self, session, account, name, email):
        upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[
                {"source": "attr", "source_id": email, "full_name": name, "emails": [email]}
            ],
        )
        session.commit()
        return session.scalar(select(Candidate).where(Candidate.full_name == name))

    def test_attributing_links_the_thread_and_clears_the_flag(
        self, client, session, account, fake_channel
    ):
        person = self._person(session, account, "Ada Attr", "ada-attr@example.com")
        thread = open_thread(
            session, org_id=account["org_id"], channel=WEB, consent_messaging=True
        )
        thread.review_needed = True
        thread.review_reason = UNATTRIBUTED_REASON
        thread.record = {
            **thread.record,
            "spec": {**thread.record["spec"], "job_titles": ["machine learning engineer"]},
        }
        session.commit()

        out = client.patch(
            f"/v1/intake/threads/{thread.id}",
            json={"candidate_id": person.id},
            headers=account["headers"],
        )
        assert out.status_code == 200
        assert out.json()["candidate_id"] == person.id
        assert out.json()["review_needed"] is False

        # The conversation's content followed the person.
        pref = session.scalar(
            select(CandidatePreference).where(CandidatePreference.candidate_id == person.id)
        )
        assert pref is not None
        assert pref.spec["job_titles"] == ["machine learning engineer"]

    def test_an_address_owned_by_somebody_else_is_refused(
        self, client, session, account, fake_channel
    ):
        """A wrong match here staples a conversation to the wrong record."""
        owner = self._person(session, account, "Real Owner", "shared@example.com")
        other = self._person(session, account, "Someone Else", "else@example.com")
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=WEB,
            consent_messaging=True,
            contact_email="shared@example.com",
        )
        session.commit()

        refused = client.patch(
            f"/v1/intake/threads/{thread.id}",
            json={"candidate_id": other.id},
            headers=account["headers"],
        )
        assert refused.status_code == 400
        assert "already belongs to somebody else" in refused.json()["detail"]
        # The rightful owner is still allowed.
        assert (
            client.patch(
                f"/v1/intake/threads/{thread.id}",
                json={"candidate_id": owner.id},
                headers=account["headers"],
            ).status_code
            == 200
        )

    def test_an_empty_thread_writes_no_preference(self, client, session, account, fake_channel):
        """Attributing a thread that captured nothing must not blank a record."""
        person = self._person(session, account, "Empty Attr", "empty-attr@example.com")
        pref = CandidatePreference(
            org_id=account["org_id"],
            candidate_id=person.id,
            spec={"job_titles": ["staff engineer"]},
        )
        session.add(pref)
        thread = open_thread(
            session, org_id=account["org_id"], channel=WEB, consent_messaging=True
        )
        session.commit()

        assert (
            client.patch(
                f"/v1/intake/threads/{thread.id}",
                json={"candidate_id": person.id},
                headers=account["headers"],
            ).status_code
            == 200
        )
        session.refresh(pref)
        assert pref.spec["job_titles"] == ["staff engineer"]

    def test_another_orgs_thread_is_invisible(self, client, session, account, fake_channel):
        thread = open_thread(
            session, org_id=account["org_id"], channel=WEB, consent_messaging=True
        )
        session.commit()
        rival = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival Attr",
                "email": "rival-attr@example.com",
                "password": "rival-password-123",
            },
        ).json()
        headers = {"Authorization": f"Bearer {rival['access_token']}"}
        assert (
            client.patch(
                f"/v1/intake/threads/{thread.id}",
                json={"candidate_id": "cand_whatever"},
                headers=headers,
            ).status_code
            == 404
        )


class TestWorkerPath:
    def test_the_job_runs_the_same_turn_the_route_does(
        self, session, thread, fake_channel
    ):
        from juicebox.worker import process_job

        job = enqueue(
            session,
            org_id=thread.org_id,
            kind=KIND_INTAKE_TURN,
            idempotency_key=f"{KIND_INTAKE_TURN}:{thread.id}:1",
            payload={"thread_id": thread.id},
        )
        session.flush()
        process_job(session, job)

        assert job.status == "succeeded"
        assert fake_channel.sent

    def test_a_missing_thread_is_skipped_not_retried_forever(self, session, account):
        from juicebox.worker import process_job

        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_INTAKE_TURN,
            idempotency_key=f"{KIND_INTAKE_TURN}:gone",
            payload={"thread_id": "ithread_gone"},
        )
        session.flush()
        process_job(session, job)

        # Succeeded, not failed: a thread that no longer exists is nothing to
        # retry, and parking the job would leave it in the queue forever.
        assert job.status == "succeeded"
        assert job.last_error == ""


# --------------------------------------------------------------------------
# completion
# --------------------------------------------------------------------------


class TestCompletion:
    def test_a_completed_thread_says_nothing_further(
        self, session, identified, fake_channel
    ):
        identified.status = STATUS_COMPLETE
        session.flush()
        before = len(fake_channel.sent)

        result = _say(session, identified, "one more thing")

        assert result.action == ACTION_COMPLETED
        assert len(fake_channel.sent) == before

    def test_the_wrap_up_is_sent_once_and_the_thread_closes(
        self, session, identified, fake_channel
    ):
        identified.record = _filled_required()
        identified.asked_counts = {
            key: MAX_ASKS_PER_FIELD for key in coverage(identified.record).enrichment_missing
        }
        session.flush()
        run_turn(session, thread=identified)

        result = _say(session, identified, "that's everything")

        assert result.action == ACTION_COMPLETED
        assert identified.status == STATUS_COMPLETE
        assert fake_channel.bodies[-1] == prompts.WRAP_UP_COMPLETE


# --------------------------------------------------------------------------
# Linq webhook
# --------------------------------------------------------------------------


LINQ_WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"linq-test-webhook-secret!!").decode()
LINQ_OUR_NUMBER = "+14155559001"
LINQ_CANDIDATE = "+14155550123"
LINQ_CHAT_ID = "8f392755-6865-4b18-880a-227f9d8b458f"
LINQ_MSG_ID = "89e3566e-1d13-49e5-a8ee-48490d5bfeb7"


@pytest.fixture
def linq_signing_env(monkeypatch):
    monkeypatch.setenv("LINQ_WEBHOOK_SECRET", LINQ_WEBHOOK_SECRET)
    monkeypatch.setenv("LINQ_API_KEY", "linq_test_api_key")
    monkeypatch.setenv("LINQ_FROM_NUMBER", LINQ_OUR_NUMBER)
    monkeypatch.setenv("LINQ_ENABLED", "true")
    from juicebox.config import reset_settings_cache
    from juicebox.messaging.channels import reset_channels

    reset_settings_cache()
    reset_channels()
    yield
    reset_settings_cache()
    reset_channels()


@pytest.fixture
def linq_number(session, account):
    row = IntakeNumber(
        org_id=account["org_id"],
        provider="linq",
        phone=LINQ_OUR_NUMBER,
        label="linq intake",
    )
    session.add(row)
    session.commit()
    return row


def linq_payload(**overrides):
    data = {
        "chat_id": LINQ_CHAT_ID,
        "from": LINQ_CANDIDATE,
        "is_group": False,
        "message": {
            "id": LINQ_MSG_ID,
            "parts": [{"type": "text", "value": "Senior backend engineer in SF"}],
        },
        "recipient_handle": {"handle": LINQ_OUR_NUMBER},
    }
    data.update(overrides)
    return {
        "api_version": "v3",
        "event_type": "message.received",
        "event_id": "evt-1",
        "data": data,
    }


def post_linq(client, payload, *, body=None, webhook_id="wh-1", timestamp=None):
    raw = body if body is not None else json.dumps(payload).encode()
    ts = str(timestamp if timestamp is not None else int(time.time()))
    signature = sign_webhook(
        raw_body=raw,
        webhook_id=webhook_id,
        timestamp=ts,
        secret=LINQ_WEBHOOK_SECRET,
    )
    return client.post(
        "/v1/intake/linq/webhook",
        content=raw,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": ts,
            "webhook-signature": signature,
            "Content-Type": "application/json",
        },
    )


class TestLinqSignature:
    @pytest.fixture(autouse=True)
    def _linq_env(self, linq_signing_env):
        yield

    def test_a_valid_signature_is_accepted(self, client, linq_number):
        assert post_linq(client, linq_payload()).status_code == 200

    def test_a_missing_signature_is_refused(self, client, linq_number):
        response = client.post("/v1/intake/linq/webhook", json=linq_payload())
        assert response.status_code == 401

    def test_a_tampered_body_is_refused(self, client, linq_number):
        raw = json.dumps(linq_payload()).encode()
        signature = sign_webhook(
            raw_body=raw, webhook_id="wh-1", timestamp=str(int(time.time())), secret=LINQ_WEBHOOK_SECRET
        )
        tampered = json.dumps(linq_payload(message={"id": "other", "parts": []})).encode()
        response = client.post(
            "/v1/intake/linq/webhook",
            content=tampered,
            headers={"webhook-id": "wh-1", "webhook-timestamp": str(int(time.time())), "webhook-signature": signature},
        )
        assert response.status_code == 401

    def test_re_serialising_the_body_breaks_verification(self, client, linq_number):
        raw = json.dumps(linq_payload(), separators=(",", ":")).encode()
        ts = str(int(time.time()))
        signature = sign_webhook(
            raw_body=raw, webhook_id="wh-1", timestamp=ts, secret=LINQ_WEBHOOK_SECRET
        )
        respaced = json.dumps(json.loads(raw), indent=2).encode()
        response = client.post(
            "/v1/intake/linq/webhook",
            content=respaced,
            headers={"webhook-id": "wh-1", "webhook-timestamp": ts, "webhook-signature": signature},
        )
        assert response.status_code == 401

    def test_a_wrong_key_is_refused(self, client, linq_number):
        wrong = "whsec_" + base64.b64encode(b"wrong-key-wrong-key-wrong-key!!").decode()
        raw = json.dumps(linq_payload()).encode()
        ts = str(int(time.time()))
        signature = sign_webhook(raw_body=raw, webhook_id="wh-1", timestamp=ts, secret=wrong)
        response = client.post(
            "/v1/intake/linq/webhook",
            content=raw,
            headers={"webhook-id": "wh-1", "webhook-timestamp": ts, "webhook-signature": signature},
        )
        assert response.status_code == 401

    def test_a_stale_timestamp_is_refused(self, client, linq_number):
        old = int(time.time()) - 6 * 60
        assert post_linq(client, linq_payload(), timestamp=old).status_code == 401

    def test_a_future_timestamp_is_refused(self, client, linq_number):
        ahead = int(time.time()) + 6 * 60
        assert post_linq(client, linq_payload(), timestamp=ahead).status_code == 401

    @pytest.mark.parametrize("header", ["", "garbage", "v1,notbase64!!!"])
    def test_a_malformed_header_is_refused(self, client, linq_number, header):
        raw = json.dumps(linq_payload()).encode()
        response = client.post(
            "/v1/intake/linq/webhook",
            content=raw,
            headers={
                "webhook-id": "wh-1",
                "webhook-timestamp": str(int(time.time())),
                "webhook-signature": header,
            },
        )
        assert response.status_code == 401

    def test_no_configured_secret_refuses_everything(self, client, linq_number, monkeypatch):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("LINQ_WEBHOOK_SECRET", "")
        reset_settings_cache()
        assert post_linq(client, linq_payload()).status_code == 401

    def test_a_refused_request_writes_nothing(self, client, session, linq_number):
        client.post("/v1/intake/linq/webhook", json=linq_payload())
        assert session.scalars(select(IntakeThread)).all() == []


class TestLinqWebhook:
    @pytest.fixture(autouse=True)
    def _linq_env(self, linq_signing_env):
        yield

    def test_the_payload_cannot_choose_the_org(
        self, client, session, linq_number, account
    ):
        payload = linq_payload()
        payload["org_id"] = "org_attacker"
        payload["data"]["org_id"] = "org_attacker"
        assert post_linq(client, payload).status_code == 200
        thread = session.scalars(select(IntakeThread)).one()
        assert thread.org_id == account["org_id"]

    def test_an_unclaimed_number_is_refused(self, client, session):
        assert post_linq(client, linq_payload()).status_code == 404
        assert session.scalars(select(IntakeThread)).all() == []

    def test_a_deactivated_number_is_refused(self, client, session, linq_number):
        linq_number.active = False
        session.commit()
        assert post_linq(client, linq_payload()).status_code == 404

    def test_a_duplicate_message_id_is_a_no_op(self, client, session, linq_number):
        assert post_linq(client, linq_payload()).status_code == 200
        assert post_linq(client, linq_payload()).json()["duplicate"] is True
        jobs = session.scalars(select(Job).where(Job.kind == KIND_INTAKE_TURN)).all()
        assert len(jobs) == 1

    def test_group_chats_are_ignored(self, client, session, linq_number):
        payload = linq_payload(is_group=True)
        response = post_linq(client, payload)
        assert response.status_code == 200
        assert response.json()["reason"] == "group_chat"
        assert session.scalars(select(IntakeThread)).all() == []

    def test_a_media_only_message_does_not_enqueue_a_turn(self, client, session, linq_number):
        payload = linq_payload(
            message={"id": "media-only-1", "parts": [{"type": "media", "url": "https://x/y.jpg"}]}
        )
        response = post_linq(client, payload)
        assert response.status_code == 200
        assert response.json()["ignored"] is True
        assert session.scalars(select(Job).where(Job.kind == KIND_INTAKE_TURN)).all() == []

    def test_stop_suppresses_the_phone(self, client, session, linq_number):
        payload = linq_payload(
            message={"id": "stop-1", "parts": [{"type": "text", "value": "STOP"}]}
        )
        assert post_linq(client, payload).status_code == 200
        thread = session.scalars(select(IntakeThread)).one()
        assert thread.status == "stopped"

    def test_help_replies_without_enqueuing_a_turn(self, client, session, linq_number, fake_channel):
        set_channel(LINQ, fake_channel)
        payload = linq_payload(
            message={"id": "help-1", "parts": [{"type": "text", "value": "help"}]}
        )
        response = post_linq(client, payload)
        assert response.status_code == 200
        assert response.json()["help"] is True
        assert session.scalars(select(Job).where(Job.kind == KIND_INTAKE_TURN)).all() == []
        assert fake_channel.bodies == [prompts.HELP_REPLY]

    def test_linq_disabled_returns_503(self, client, linq_number, monkeypatch):
        monkeypatch.setenv("LINQ_ENABLED", "false")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        assert post_linq(client, linq_payload()).status_code == 503

    def test_the_sender_thread_cap_refuses_excess_new_threads(
        self, client, session, linq_number, monkeypatch
    ):
        from juicebox.routers import messaging as messaging_router

        monkeypatch.setattr(messaging_router, "MAX_NEW_THREADS_PER_SENDER_PER_HOUR", 2)

        for index in range(2):
            payload = linq_payload(
                chat_id=f"chat-{index}",
                message={
                    "id": f"msg-{index}",
                    "parts": [{"type": "text", "value": f"hello {index}"}],
                },
            )
            assert post_linq(client, payload).status_code == 200
            thread = session.scalars(
                select(IntakeThread).where(IntakeThread.status == "active")
            ).one()
            thread.status = "complete"
            session.commit()

        blocked = post_linq(
            client,
            linq_payload(
                chat_id="chat-blocked",
                message={
                    "id": "msg-blocked",
                    "parts": [{"type": "text", "value": "one more"}],
                },
            ),
        )
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == "Too many new conversations from this sender"
