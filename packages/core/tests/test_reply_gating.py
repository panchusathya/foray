"""A reply ends automated mail to that person -- whichever sequence, mailbox
or forwarded copy it arrived through -- and a date in it is remembered.

Two prod incidents drove this. A candidate wrote "In Japan until 9/10 please
f/u after", the model called it out_of_office (the one intent that keeps a
sequence alive), and step 3 went out on schedule. Another replied
"interested" to one sequence and was mailed twice more by a re-created copy
of it that had never heard about the reply.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from juicebox.integrations.sync import sync_mailbox
from juicebox.jobs import KIND_OUTREACH_FOLLOW_UP
from juicebox.llm.client import FakeTransport, LLMClient, set_llm
from juicebox.llm.prompts import reply_prompt
from juicebox.models import (
    Enrollment,
    Event,
    Job,
    MailboxConnection,
    Message,
    Sequence,
    SequenceStep,
)
from juicebox.outreach.follow_ups import (
    RESULT_ALREADY_SENT,
    RESULT_SENT,
    RESULT_SUPERSEDED,
    send_follow_up_reminder,
)
from juicebox.outreach.replies import (
    HUMAN_INTENTS,
    classify_reply,
    heuristic_classify,
    parse_follow_up_date,
)
from juicebox.outreach.sequencer import (
    enroll_candidates,
    process_enrollment,
    recent_human_replies,
    record_reply,
    run_due,
    schedule_step_at,
)
from juicebox.worker import HANDLERS, process_job
from test_mailbox_sync import (
    FakeDeltaClient,
    _inbound,
    graph_provider,
    install_graph_sender,
)

RECEIVED = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
LUKE = "In Japan until 9/10 please f/u after"


def _ada(fixture):
    return next(c for c in fixture["candidates"] if c.full_name == "Ada Okonkwo")


def _send_first(session, fixture, candidate=None):
    sequence = fixture["sequence"]
    who = candidate or _ada(fixture)
    [enrollment] = enroll_candidates(
        session, sequence=sequence, candidate_ids=[who.id], project_id=sequence.project_id
    )
    session.commit()
    run_due(session, org_id=sequence.org_id, sequence_id=sequence.id)
    session.commit()
    return session.query(Message).filter_by(direction="outbound", enrollment_id=enrollment.id).one()


def _second_sequence(
    session, fixture, *, name="Three touch (copy)", stop_on_reply=True
) -> Sequence:
    """The re-created campaign: same steps, a new sequence row."""
    original = fixture["sequence"]
    copy = Sequence(
        org_id=original.org_id,
        project_id=original.project_id,
        name=name,
        from_email=original.from_email,
        from_name=original.from_name,
        personalize=False,
        stop_on_reply=stop_on_reply,
    )
    session.add(copy)
    session.flush()
    for step in original.steps:
        session.add(
            SequenceStep(
                sequence_id=copy.id,
                step_order=step.step_order,
                delay_days=step.delay_days,
                subject_template=step.subject_template,
                body_template=step.body_template,
            )
        )
    session.commit()
    return copy


@pytest.fixture
def synced_org(session, account, outreach_fixture):
    """A connected mailbox that has sent step 0 to Ada via Graph -- the same
    shape as test_mailbox_sync's fixture, local so pytest can see it."""
    from juicebox.integrations.crypto import encrypt_json

    connection = MailboxConnection(
        org_id=account["org_id"],
        address="recruiter@goforay.io",
        token_blob=encrypt_json({"access_token": "at", "refresh_token": "rt", "expires_at": 2**40}),
        inbox_delta_link="https://graph.test/delta?token=init",
    )
    session.add(connection)
    session.flush()
    sequence = outreach_fixture["sequence"]
    sequence.sender_mode = "fixed"
    sequence.mailbox_id = connection.id

    install_graph_sender()
    ada = _ada(outreach_fixture)
    enroll_candidates(
        session, sequence=sequence, candidate_ids=[ada.id], project_id=sequence.project_id
    )
    session.commit()
    run_due(session, org_id=sequence.org_id)
    session.commit()
    return {"connection": connection, "sequence": sequence, "ada": ada}


def _events(session, type_):
    return session.query(Event).filter_by(type=type_).all()


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


class TestClassification:
    def test_a_human_absence_note_is_not_now_with_the_date(self):
        verdict = heuristic_classify("Re: role", LUKE, received_at=RECEIVED)
        assert verdict.intent == "not_now"
        assert verdict.follow_up_after == date(2026, 9, 10)
        assert verdict.should_stop_sequence is True

    def test_an_auto_responder_is_out_of_office_and_keeps_its_date(self):
        verdict = heuristic_classify(
            "Automatic reply: Enterprise AE", "I will be back on 9/15.", received_at=RECEIVED
        )
        assert verdict.intent == "out_of_office"
        assert verdict.follow_up_after == date(2026, 9, 15)
        assert verdict.should_stop_sequence is False

    def test_templated_wording_counts_as_automated_without_a_subject_marker(self):
        verdict = heuristic_classify(
            "Re: role",
            "I'm traveling and won't have consistent access to email until Sept. 9th, 2026.",
            received_at=RECEIVED,
        )
        assert verdict.intent == "out_of_office"
        assert verdict.follow_up_after == date(2026, 9, 9)

    def test_asking_for_details_is_a_question(self):
        verdict = heuristic_classify(
            "Re: role", "Give me more details about the role, position and location"
        )
        assert verdict.intent == "question"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("until 9/10", date(2026, 9, 10)),
            ("back 9/10/26", date(2026, 9, 10)),
            ("Sept. 9th, 2026", date(2026, 9, 9)),
            ("September 9", date(2026, 9, 9)),
            ("15 September", date(2026, 9, 15)),
            ("2026-10-01", date(2026, 10, 1)),
            ("in six weeks", date(2026, 10, 14)),
            ("next month", date(2026, 10, 2)),
            ("early January", date(2027, 1, 1)),
            ("nothing here", None),
            ("2026-08-01", None),  # in the past: not a follow-up
        ],
    )
    def test_parse_follow_up_date(self, text, expected):
        assert parse_follow_up_date(text, RECEIVED) == expected

    def test_year_rolls_forward_when_the_day_has_passed(self):
        assert parse_follow_up_date("until 1/15", datetime(2026, 12, 20, tzinfo=UTC)) == date(
            2027, 1, 15
        )

    def test_prompt_carries_the_received_date(self):
        prompt = reply_prompt(subject="Re", body="x", received_at=RECEIVED)
        assert "<received_on>2026-09-02 (Wednesday)</received_on>" in prompt

    def test_the_model_stop_flag_is_never_trusted(self):
        transport = FakeTransport(
            default={
                "intent": "not_now",
                "confidence": 0.9,
                "should_stop_sequence": False,
                "follow_up_after": "2026-09-10",
                "summary": "away",
                "suggested_next_step": "",
            }
        )
        set_llm(LLMClient(transport=transport, enabled=True, model="test-model"))
        try:
            verdict = classify_reply("Re: role", LUKE, received_at=RECEIVED)
        finally:
            set_llm(None)
        assert verdict.classified_by == "llm"
        assert verdict.should_stop_sequence is True
        assert verdict.follow_up_after == date(2026, 9, 10)

    def test_an_unparseable_model_date_falls_back_to_the_body(self):
        transport = FakeTransport(
            default={
                "intent": "not_now",
                "confidence": 0.9,
                "follow_up_after": "after 9/10",
                "summary": "",
                "suggested_next_step": "",
            }
        )
        set_llm(LLMClient(transport=transport, enabled=True, model="test-model"))
        try:
            verdict = classify_reply("Re: role", LUKE, received_at=RECEIVED)
        finally:
            set_llm(None)
        assert verdict.follow_up_after == date(2026, 9, 10)

    def test_human_intents_exclude_automation(self):
        assert "out_of_office" not in HUMAN_INTENTS and "bounce" not in HUMAN_INTENTS
        assert {"interested", "not_now", "question", "unsubscribe"} <= HUMAN_INTENTS


# --------------------------------------------------------------------------
# record_reply
# --------------------------------------------------------------------------


class TestRecordReply:
    def test_human_not_now_stops_and_stores_the_follow_up_date(
        self, session, outreach_fixture, mailbox
    ):
        message = _send_first(session, outreach_fixture)
        record_reply(session, message=message, subject="Re: role", body=LUKE, received_at=RECEIVED)
        session.commit()

        enrollment = session.query(Enrollment).one()
        assert enrollment.status == "replied"
        assert enrollment.stopped_reason == "not_now"
        assert enrollment.next_send_at is None
        assert enrollment.follow_up_after == date(2026, 9, 10)
        assert enrollment.replied_at is not None
        inbound = session.query(Message).filter_by(direction="inbound").one()
        assert inbound.sent_at.replace(tzinfo=UTC) == RECEIVED

        job = session.query(Job).filter_by(kind=KIND_OUTREACH_FOLLOW_UP).one()
        # The day AFTER the date they named -- "after 9/10" is the 11th.
        assert job.run_at.date() == date(2026, 9, 11)
        assert job.payload["enrollment_id"] == enrollment.id
        assert job.payload["inbound_message_id"] == inbound.id

    def test_automated_out_of_office_with_a_date_snoozes_past_the_return(
        self, session, outreach_fixture, mailbox
    ):
        message = _send_first(session, outreach_fixture)
        record_reply(
            session,
            message=message,
            subject="Automatic reply: role",
            body="I am out of the office until 9/30 and will respond when I return.",
            received_at=RECEIVED,
        )
        session.commit()

        enrollment = session.query(Enrollment).one()
        assert enrollment.status == "active"
        assert enrollment.next_send_at.date() == date(2026, 10, 1)
        assert enrollment.snoozed_until is not None
        # Nothing sends while they are away.
        summary = run_due(session, org_id=message.org_id, now=datetime(2026, 9, 25, tzinfo=UTC))
        assert summary.sent == 0
        assert session.query(Job).filter_by(kind=KIND_OUTREACH_FOLLOW_UP).count() == 0

    def test_out_of_office_without_a_date_gets_the_default_snooze(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        message = _send_first(session, outreach_fixture)
        before = session.query(Enrollment).one().next_send_at
        now = datetime.now(UTC)
        record_reply(
            session,
            message=message,
            subject="Automatic reply: role",
            body="I am currently out of the office.",
            now=now,
        )
        session.commit()
        enrollment = session.query(Enrollment).one()
        assert enrollment.status == "active"
        assert enrollment.next_send_at.replace(tzinfo=UTC) >= now + timedelta(days=3) - timedelta(
            seconds=5
        )
        assert enrollment.next_send_at.replace(tzinfo=UTC) > before.replace(tzinfo=UTC)

    def test_a_zero_default_snooze_leaves_the_schedule_alone(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        monkeypatch.setenv("OUTREACH_OOO_DEFAULT_SNOOZE_DAYS", "0")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        message = _send_first(session, outreach_fixture)
        before = session.query(Enrollment).one().next_send_at
        record_reply(
            session,
            message=message,
            subject="Automatic reply: role",
            body="I am currently out of the office.",
        )
        session.commit()
        assert session.query(Enrollment).one().next_send_at == before

    def test_a_human_reply_stops_every_active_enrollment_for_the_candidate(
        self, session, outreach_fixture, mailbox
    ):
        ada = _ada(outreach_fixture)
        copy = _second_sequence(session, outreach_fixture)
        enroll_candidates(session, sequence=copy, candidate_ids=[ada.id])
        session.commit()
        message = _send_first(session, outreach_fixture)

        record_reply(session, message=message, subject="Re", body="Happy to chat next week!")
        session.commit()

        by_sequence = {e.sequence_id: e for e in session.query(Enrollment).all()}
        assert by_sequence[outreach_fixture["sequence"].id].stopped_reason == "interested"
        sibling = by_sequence[copy.id]
        assert sibling.status == "replied"
        assert sibling.stopped_reason == "replied_elsewhere"
        assert sibling.next_send_at is None
        assert sibling.replied_at is not None
        stopped = _events(session, "outreach.stopped")
        assert len(stopped) == 1 and stopped[0].subject_id == sibling.id

    def test_a_sibling_that_does_not_stop_on_reply_keeps_going(
        self, session, outreach_fixture, mailbox
    ):
        ada = _ada(outreach_fixture)
        copy = _second_sequence(session, outreach_fixture, stop_on_reply=False)
        enroll_candidates(session, sequence=copy, candidate_ids=[ada.id])
        session.commit()
        message = _send_first(session, outreach_fixture)

        record_reply(session, message=message, subject="Re", body="Happy to chat next week!")
        session.commit()
        sibling = session.query(Enrollment).filter_by(sequence_id=copy.id).one()
        assert sibling.status == "active"

    def test_unsubscribe_stops_siblings_regardless_of_their_setting(
        self, session, outreach_fixture, mailbox
    ):
        ada = _ada(outreach_fixture)
        copy = _second_sequence(session, outreach_fixture, stop_on_reply=False)
        enroll_candidates(session, sequence=copy, candidate_ids=[ada.id])
        session.commit()
        message = _send_first(session, outreach_fixture)

        record_reply(session, message=message, subject="Re", body="Please unsubscribe me.")
        session.commit()
        sibling = session.query(Enrollment).filter_by(sequence_id=copy.id).one()
        assert sibling.status == "replied" and sibling.stopped_reason == "unsubscribe"

    def test_a_reply_on_a_hand_sent_thread_still_stops_active_enrollments(
        self, session, outreach_fixture, mailbox
    ):
        ada = _ada(outreach_fixture)
        sequence = outreach_fixture["sequence"]
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        hand_sent = Message(
            org_id=sequence.org_id,
            candidate_id=ada.id,
            direction="outbound",
            source="zoho",
            from_address="recruiter@goforay.io",
            to_address="ada@example.com",
            subject="Quick one",
            body="Ada, are you open to a chat?",
            status="sent",
        )
        session.add(hand_sent)
        session.commit()

        record_reply(session, message=hand_sent, subject="Re", body="Sure, happy to chat.")
        session.commit()
        enrollment = session.query(Enrollment).one()
        assert enrollment.status == "replied"
        assert enrollment.stopped_reason == "replied_elsewhere"

    def test_the_same_words_within_a_few_days_are_recorded_once(
        self, session, outreach_fixture, mailbox
    ):
        message = _send_first(session, outreach_fixture)
        first = record_reply(
            session,
            message=message,
            subject="Re",
            body="Happy to chat next week!",
            received_at=RECEIVED,
        )
        again = record_reply(
            session,
            message=message,
            subject="Fwd: Re",
            body="Happy   to chat next week!",
            received_at=RECEIVED + timedelta(minutes=1),
        )
        session.commit()
        assert first.classified_by == "heuristic"
        assert again.classified_by == "duplicate"
        assert again.intent == "interested"
        assert session.query(Message).filter_by(direction="inbound").count() == 1
        assert len(_events(session, "outreach.replied")) == 1

    def test_reply_event_carries_the_date_and_the_sibling_count(
        self, session, outreach_fixture, mailbox
    ):
        message = _send_first(session, outreach_fixture)
        record_reply(session, message=message, subject="Re", body=LUKE, received_at=RECEIVED)
        session.commit()
        [event] = _events(session, "outreach.replied")
        assert event.payload["follow_up_after"] == "2026-09-10"
        assert event.payload["stopped_elsewhere"] == 0


# --------------------------------------------------------------------------
# the send-time and enrol-time guards
# --------------------------------------------------------------------------


class TestQuietPeriodGuards:
    def _reply_then_second_sequence(self, session, fixture, *, stop_on_reply=True):
        """Brett's shape: reply to A, THEN get enrolled in B."""
        ada = _ada(fixture)
        message = _send_first(session, fixture)
        record_reply(session, message=message, subject="Re", body="Happy to chat next week!")
        session.commit()
        copy = _second_sequence(session, fixture, stop_on_reply=stop_on_reply)
        return ada, copy

    def test_enrolment_skips_someone_who_replied_recently(self, session, outreach_fixture, mailbox):
        ada, copy = self._reply_then_second_sequence(session, outreach_fixture)
        created = enroll_candidates(session, sequence=copy, candidate_ids=[ada.id])
        session.commit()
        assert created == []
        assert session.query(Enrollment).filter_by(sequence_id=copy.id).count() == 0

    def test_the_guard_can_be_switched_off_per_call(self, session, outreach_fixture, mailbox):
        ada, copy = self._reply_then_second_sequence(session, outreach_fixture)
        created = enroll_candidates(
            session, sequence=copy, candidate_ids=[ada.id], skip_recent_repliers=False
        )
        session.commit()
        assert len(created) == 1

    def test_a_due_step_is_refused_after_a_recent_reply_elsewhere(
        self, session, outreach_fixture, mailbox
    ):
        ada, copy = self._reply_then_second_sequence(session, outreach_fixture)
        [enrollment] = enroll_candidates(
            session, sequence=copy, candidate_ids=[ada.id], skip_recent_repliers=False
        )
        session.commit()
        sent_before = len(mailbox.outbox)

        summary = run_due(session, org_id=copy.org_id, sequence_id=copy.id)
        session.commit()

        assert summary.sent == 0
        assert len(mailbox.outbox) == sent_before
        session.refresh(enrollment)
        assert enrollment.status == "replied"
        assert enrollment.stopped_reason == "replied_elsewhere"
        assert enrollment.replied_at is not None
        assert any(e.subject_id == enrollment.id for e in _events(session, "outreach.stopped"))

    def test_a_dry_run_does_not_stop_a_guarded_enrollment(self, session, outreach_fixture, mailbox):
        ada, copy = self._reply_then_second_sequence(session, outreach_fixture)
        [enrollment] = enroll_candidates(
            session, sequence=copy, candidate_ids=[ada.id], skip_recent_repliers=False
        )
        session.commit()
        status, message = process_enrollment(
            session, enrollment, now=datetime.now(UTC), dry_run=True
        )
        assert status == "skipped" and message is None
        assert enrollment.status == "active"

    def test_quiet_days_zero_disables_both_guards(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        monkeypatch.setenv("OUTREACH_REPLY_QUIET_DAYS", "0")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        ada, copy = self._reply_then_second_sequence(session, outreach_fixture)
        created = enroll_candidates(session, sequence=copy, candidate_ids=[ada.id])
        session.commit()
        assert len(created) == 1
        summary = run_due(session, org_id=copy.org_id, sequence_id=copy.id)
        assert summary.sent == 1

    def test_a_sequence_that_ignores_replies_still_mails_its_own_replier(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        sequence.stop_on_reply = False
        session.commit()
        message = _send_first(session, outreach_fixture)
        record_reply(session, message=message, subject="Re", body="Happy to chat next week!")
        session.commit()
        enrollment = session.query(Enrollment).one()
        assert enrollment.status == "active"
        enrollment.next_send_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()

        summary = run_due(session, org_id=sequence.org_id)
        assert summary.sent == 1

    def test_recent_human_replies_ignores_automation_and_old_mail(
        self, session, outreach_fixture, mailbox
    ):
        message = _send_first(session, outreach_fixture)
        ada = _ada(outreach_fixture)
        long_ago = datetime.now(UTC) - timedelta(days=45)
        record_reply(
            session, message=message, subject="Re", body="Happy to chat!", received_at=long_ago
        )
        record_reply(
            session,
            message=message,
            subject="Automatic reply: role",
            body="I am currently out of the office.",
            received_at=datetime.now(UTC),
        )
        session.commit()
        hits = recent_human_replies(
            session, org_id=message.org_id, candidate_ids=[ada.id], now=datetime.now(UTC), days=30
        )
        assert hits == {}

    def test_snooze_is_cleared_when_the_step_sends(self, session, outreach_fixture, mailbox):
        message = _send_first(session, outreach_fixture)
        record_reply(
            session,
            message=message,
            subject="Automatic reply: role",
            body="I am currently out of the office until 9/30.",
            received_at=RECEIVED,
        )
        session.commit()
        enrollment = session.query(Enrollment).one()
        assert enrollment.snoozed_until is not None
        run_due(session, org_id=message.org_id, now=datetime(2026, 10, 2, tzinfo=UTC))
        session.commit()
        session.refresh(enrollment)
        assert enrollment.snoozed_until is None

    def test_ignore_delay_keeps_the_time_snap(self):
        step = SequenceStep(
            step_order=1,
            delay_days=3,
            delay_unit="business_days",
            send_at_time="09:00",
            send_timezone="America/New_York",
        )
        base = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)  # a Friday
        due = schedule_step_at(step, base=base, ignore_delay=True)
        assert due == datetime(2026, 9, 11, 13, 0, tzinfo=UTC)  # 09:00 New York, same day


class TestEnrollEndpoint:
    def test_recent_repliers_are_reported_and_force_overrides(
        self, client, loaded_org, mailbox, session
    ):
        headers = loaded_org["headers"]
        sequence = client.post(
            "/v1/sequences",
            json={
                "name": "Guarded",
                "steps": [
                    {
                        "step_order": 0,
                        "delay_days": 0,
                        "subject_template": "Hi",
                        "body_template": "x",
                    }
                ],
            },
            headers=headers,
        ).json()
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]
        first = client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate_id], "send_now": True},
            headers=headers,
        ).json()
        assert len(first["enrollments"]) == 1 and first["skipped"] == []
        outbound = session.query(Message).filter_by(direction="outbound").one()
        record_reply(session, message=outbound, subject="Re", body="Happy to chat next week!")
        session.commit()

        second = client.post(
            "/v1/sequences",
            json={
                "name": "Guarded copy",
                "steps": [
                    {
                        "step_order": 0,
                        "delay_days": 0,
                        "subject_template": "Hi",
                        "body_template": "x",
                    }
                ],
            },
            headers=headers,
        ).json()
        response = client.post(
            f"/v1/sequences/{second['id']}/enroll",
            json={"candidate_ids": [candidate_id]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["enrollments"] == []
        assert len(body["skipped"]) == 1
        skipped = body["skipped"][0]
        assert skipped["candidate_id"] == candidate_id
        assert skipped["reason"] == "replied_recently"
        assert skipped["reply_intent"] == "interested"
        assert skipped["quiet_days"] == 30

        forced = client.post(
            f"/v1/sequences/{second['id']}/enroll",
            json={"candidate_ids": [candidate_id], "force": True},
            headers=headers,
        ).json()
        assert len(forced["enrollments"]) == 1 and forced["skipped"] == []

    def test_enrollment_rows_expose_the_reply_state(self, client, loaded_org, mailbox, session):
        headers = loaded_org["headers"]
        sequence = client.post(
            "/v1/sequences",
            json={
                "name": "Dated",
                "steps": [
                    {
                        "step_order": 0,
                        "delay_days": 0,
                        "subject_template": "Hi",
                        "body_template": "x",
                    }
                ],
            },
            headers=headers,
        ).json()
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]
        client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate_id], "send_now": True},
            headers=headers,
        )
        outbound = session.query(Message).filter_by(direction="outbound").one()
        record_reply(session, message=outbound, subject="Re", body=LUKE, received_at=RECEIVED)
        session.commit()

        [row] = client.get(f"/v1/sequences/{sequence['id']}/enrollments", headers=headers).json()
        assert row["stopped_reason"] == "not_now"
        assert row["follow_up_after"] == "2026-09-10"
        assert row["replied_at"] is not None
        assert row["follow_up_reminder_sent_at"] is None


# --------------------------------------------------------------------------
# mailbox sync: forwarded copies and mailbox-scoped matching
# --------------------------------------------------------------------------


def _central_mailbox(session, org_id, address="central@goforay.io") -> MailboxConnection:
    from juicebox.integrations.crypto import encrypt_json

    connection = MailboxConnection(
        org_id=org_id,
        address=address,
        token_blob=encrypt_json({"access_token": "at", "refresh_token": "rt", "expires_at": 2**40}),
        inbox_delta_link="https://graph.test/delta?token=init",
    )
    session.add(connection)
    session.flush()
    return connection


class TestForwardedCopies:
    def test_a_copy_addressed_to_a_sibling_mailbox_is_skipped(self, session, synced_org):
        central = _central_mailbox(session, synced_org["connection"].org_id)
        session.commit()
        payload = _inbound(conversation_id="", graph_id="fwd-1")
        payload["toRecipients"] = [{"emailAddress": {"address": "recruiter@goforay.io"}}]
        fake = FakeDeltaClient()
        fake.queue(payload)

        summary = sync_mailbox(session, central, provider=graph_provider(lambda db, conn: fake))
        session.commit()

        assert summary.forwarded_skipped == 1 and summary.matched == 0
        assert session.query(Message).filter_by(direction="inbound").count() == 0

    def test_the_original_in_its_own_mailbox_is_still_recorded(self, session, synced_org):
        _central_mailbox(session, synced_org["connection"].org_id)
        session.commit()
        payload = _inbound()
        payload["toRecipients"] = [{"emailAddress": {"address": "recruiter@goforay.io"}}]
        fake = FakeDeltaClient()
        fake.queue(payload)

        summary = sync_mailbox(
            session, synced_org["connection"], provider=graph_provider(lambda db, conn: fake)
        )
        assert summary.matched == 1 and summary.forwarded_skipped == 0

    def test_a_copy_without_recipients_dedupes_by_content(self, session, synced_org):
        central = _central_mailbox(session, synced_org["connection"].org_id)
        session.commit()
        fake = FakeDeltaClient()
        fake.queue(_inbound())
        sync_mailbox(
            session, synced_org["connection"], provider=graph_provider(lambda db, conn: fake)
        )
        session.commit()

        copy = _inbound(conversation_id="", graph_id="fwd-2")
        fake_central = FakeDeltaClient()
        fake_central.queue(copy)
        summary = sync_mailbox(
            session, central, provider=graph_provider(lambda db, conn: fake_central)
        )
        session.commit()

        assert summary.matched == 1  # matched, but record_reply saw the duplicate
        assert session.query(Message).filter_by(direction="inbound").count() == 1

    def test_address_fallback_prefers_the_mailbox_the_reply_landed_in(
        self, session, synced_org, outreach_fixture
    ):
        """Ada is in two sequences sent from two mailboxes. A reply with no
        thread id arriving in the SECOND mailbox belongs to the second
        sequence's outbound, not the org-wide newest one."""
        org_id = synced_org["connection"].org_id
        second_box = _central_mailbox(session, org_id, address="second@goforay.io")
        copy = _second_sequence(session, outreach_fixture)
        copy.sender_mode = "fixed"
        copy.mailbox_id = second_box.id
        session.commit()
        install_graph_sender(
            native_id="out-2", rfc_id="<out-2@outlook.com>", conversation_id="conv-2"
        )
        ada = synced_org["ada"]
        enroll_candidates(
            session, sequence=copy, candidate_ids=[ada.id], skip_recent_repliers=False
        )
        session.commit()
        run_due(session, org_id=org_id, sequence_id=copy.id)
        session.commit()
        # Make the FIRST sequence's outbound the newest row, so an org-wide
        # "newest to this address" guess would pick the wrong one.
        first_outbound = (
            session.query(Message)
            .filter_by(
                direction="outbound",
                enrollment_id=session.query(Enrollment)
                .filter_by(sequence_id=outreach_fixture["sequence"].id)
                .one()
                .id,
            )
            .one()
        )
        first_outbound.created_at = datetime.now(UTC) + timedelta(hours=1)
        session.commit()

        fake = FakeDeltaClient()
        fake.queue(_inbound(conversation_id="", graph_id="in-second"))
        sync_mailbox(session, second_box, provider=graph_provider(lambda db, conn: fake))
        session.commit()

        inbound = session.query(Message).filter_by(direction="inbound").one()
        second_enrollment = session.query(Enrollment).filter_by(sequence_id=copy.id).one()
        assert inbound.enrollment_id == second_enrollment.id

    def test_received_time_reaches_the_reply(self, session, synced_org):
        fake = FakeDeltaClient()
        fake.queue(_inbound())
        sync_mailbox(
            session, synced_org["connection"], provider=graph_provider(lambda db, conn: fake)
        )
        session.commit()
        inbound = session.query(Message).filter_by(direction="inbound").one()
        assert inbound.sent_at.replace(tzinfo=UTC) == datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# the follow-up reminder
# --------------------------------------------------------------------------


class TestFollowUpReminder:
    def _not_now(self, session, synced_org):
        fake = FakeDeltaClient()
        fake.queue(_inbound(body=LUKE, graph_id="in-luke"))
        sync_mailbox(
            session, synced_org["connection"], provider=graph_provider(lambda db, conn: fake)
        )
        session.commit()
        enrollment = session.query(Enrollment).one()
        inbound = session.query(Message).filter_by(direction="inbound").one()
        return enrollment, inbound

    def test_the_reminder_goes_to_the_pinned_mailbox_once(self, session, synced_org):
        sender = install_graph_sender()
        enrollment, inbound = self._not_now(session, synced_org)
        assert enrollment.follow_up_after is not None
        sent_before = len(sender.outbox)

        result = send_follow_up_reminder(session, enrollment=enrollment, inbound=inbound)
        session.commit()

        assert result == RESULT_SENT
        email = sender.outbox[-1]
        assert len(sender.outbox) == sent_before + 1
        assert email.to == "recruiter@goforay.io" and email.from_email == "recruiter@goforay.io"
        assert "Ada Okonkwo" in email.subject
        assert "follow up after" in email.text_body
        assert LUKE in email.text_body
        assert enrollment.follow_up_reminder_sent_at is not None
        assert len(_events(session, "outreach.follow_up_reminded")) == 1

        assert (
            send_follow_up_reminder(session, enrollment=enrollment, inbound=inbound)
            == RESULT_ALREADY_SENT
        )
        assert len(sender.outbox) == sent_before + 1

    def test_a_newer_date_supersedes_the_queued_reminder(self, session, synced_org):
        install_graph_sender()
        enrollment, inbound = self._not_now(session, synced_org)
        enrollment.follow_up_after = date(2026, 10, 1)
        session.commit()
        result = send_follow_up_reminder(
            session, enrollment=enrollment, inbound=inbound, follow_up_after=date(2026, 9, 10)
        )
        assert result == RESULT_SUPERSEDED
        assert enrollment.follow_up_reminder_sent_at is None

    def test_the_worker_runs_the_queued_job(self, session, synced_org):
        sender = install_graph_sender()
        enrollment, _reply = self._not_now(session, synced_org)
        job = session.query(Job).filter_by(kind=KIND_OUTREACH_FOLLOW_UP).one()
        assert job.idempotency_key == f"{KIND_OUTREACH_FOLLOW_UP}:{enrollment.id}:2026-09-10"

        process_job(session, job, now=job.run_at)
        session.commit()

        session.refresh(job)
        assert job.status == "succeeded"
        assert "asked to hear from you" in sender.outbox[-1].subject
        session.refresh(enrollment)
        assert enrollment.follow_up_reminder_sent_at is not None

    def test_the_job_is_idempotent_per_enrollment_and_date(
        self, session, outreach_fixture, mailbox
    ):
        message = _send_first(session, outreach_fixture)
        record_reply(session, message=message, subject="Re", body=LUKE, received_at=RECEIVED)
        record_reply(
            session,
            message=message,
            subject="Re",
            body="Still in Japan until 9/10, follow up after",
            received_at=RECEIVED + timedelta(days=1),
        )
        session.commit()
        assert session.query(Job).filter_by(kind=KIND_OUTREACH_FOLLOW_UP).count() == 1

    def test_handler_is_registered(self):
        assert KIND_OUTREACH_FOLLOW_UP in HANDLERS
