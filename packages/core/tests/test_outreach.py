from datetime import timedelta

import pytest

from juicebox.models import Candidate, Enrollment, Message, utcnow
from juicebox.outreach import templates
from juicebox.outreach.compliance import is_suppressed, suppress
from juicebox.outreach.personalize import personalize
from juicebox.outreach.replies import heuristic_classify
from juicebox.outreach.sequencer import (
    enroll_candidates,
    find_message_for_reply,
    preview_step,
    record_reply,
    run_due,
    send_step,
)
from juicebox.repository import upsert_candidates
from juicebox.security import signed_tracking_token

# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------


class TestTemplates:
    def test_renders_values_and_fallbacks(self):
        rendered = templates.render(
            "Hi {{ first_name }}, about {{ role_title | the role }}",
            {"first_name": "Ada"},
        )
        assert rendered == "Hi Ada, about the role"

    def test_unknown_variable_without_fallback_collapses(self):
        assert templates.render("Hello {{ nope }}!", {}) == "Hello !"

    def test_missing_variables_are_reported(self):
        template = "{{ a }} {{ b | ok }}"
        assert templates.missing_variables(template, {}) == ["a"]

    def test_used_variables_listed(self):
        assert templates.used_variables("{{ a }} {{ b }} {{ a }}") == ["a", "b"]

    def test_html_escapes_and_paragraphs(self):
        html = templates.to_html("Hi <script>\n\nSecond para")
        assert "&lt;script&gt;" in html
        assert html.count("<p>") == 2

    def test_tracking_rewrites_links_and_adds_pixel(self):
        html = templates.add_tracking(
            '<p><a href="https://jobs.example.com/role">role</a></p>',
            base_url="http://x",
            message_id="msg_1",
            token="tok",
        )
        assert "http://x/t/c/msg_1?k=tok&u=https%3A%2F%2Fjobs.example.com%2Frole" in html
        assert "http://x/t/o/msg_1.png?k=tok" in html


# --------------------------------------------------------------------------
# reply classification
# --------------------------------------------------------------------------


class TestReplyClassification:
    @pytest.mark.parametrize(
        "body,expected",
        [
            ("Thanks but I'm not interested right now.", "not_interested"),
            ("This sounds interesting — happy to chat this week.", "interested"),
            ("I am out of office until Monday.", "out_of_office"),
            ("Please unsubscribe me from this list.", "unsubscribe"),
            ("Delivery Status Notification (Failure)", "bounce"),
            ("Not for me, but you should talk to my colleague Sam.", "referral"),
            ("What is the compensation range?", "question"),
        ],
    )
    def test_intents(self, body, expected):
        assert heuristic_classify("Re: role", body).intent == expected

    def test_only_out_of_office_keeps_the_sequence_running(self):
        assert heuristic_classify("", "out of office").should_stop_sequence is False
        assert heuristic_classify("", "not interested").should_stop_sequence is True

    def test_opt_out_beats_a_positive_phrase(self):
        verdict = heuristic_classify("", "Sounds interesting but please remove me from this list")
        assert verdict.intent == "unsubscribe"


# --------------------------------------------------------------------------
# sequencing
# --------------------------------------------------------------------------


class TestPersonalization:
    def test_heuristic_context_is_grounded_in_the_profile(self, session, outreach_fixture):
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        context = personalize(ada, outreach_fixture["project"], use_llm=False)
        assert context["first_name"] == "Ada"
        assert context["current_company"] == "Modal"
        assert "Modal" in context["opening_line"]
        assert context["personalized_by"] == "heuristic"

    def test_preview_renders_without_sending(self, session, outreach_fixture):
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        subject, body, _ = preview_step(
            session, sequence=outreach_fixture["sequence"], candidate=ada
        )
        assert subject == "Senior Machine Learning Engineer at Founding ML Engineer"
        assert body.startswith("Hi Ada,")
        assert session.query(Message).count() == 0


class TestSequenceRun:
    def test_first_step_sends_and_schedules_the_next(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(
            session, sequence=sequence, candidate_ids=[ada.id], project_id=sequence.project_id
        )
        session.commit()

        summary = run_due(session, org_id=sequence.org_id)
        session.commit()

        assert summary.sent == 1 and summary.failed == 0
        assert len(mailbox.outbox) == 1
        assert mailbox.outbox[0].to == "ada@example.com"
        assert "Hi Ada," in mailbox.outbox[0].text_body
        assert "List-Unsubscribe" in mailbox.outbox[0].headers
        assert "/t/u/" in mailbox.outbox[0].text_body

        enrollment = session.query(Enrollment).one()
        assert enrollment.current_step == 1
        assert enrollment.status == "active"
        assert enrollment.next_send_at is not None

    def test_nothing_sends_before_the_delay_elapses(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        ada = outreach_fixture["candidates"][0]
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()

        run_due(session, org_id=sequence.org_id)
        session.commit()
        run_due(session, org_id=sequence.org_id)  # immediately again
        session.commit()

        assert len(mailbox.outbox) == 1

    def test_sequence_completes_after_the_last_step(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        ada = outreach_fixture["candidates"][0]
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()

        now = utcnow()
        run_due(session, org_id=sequence.org_id, now=now)
        session.commit()
        run_due(session, org_id=sequence.org_id, now=now + timedelta(days=4))
        session.commit()

        enrollment = session.query(Enrollment).one()
        assert enrollment.status == "completed"
        assert enrollment.next_send_at is None
        assert len(mailbox.outbox) == 2

    def test_candidate_without_an_email_is_stopped_not_retried(
        self, session, account, outreach_fixture, mailbox
    ):
        upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[{"source": "test", "source_id": "no-email", "full_name": "No Email"}],
        )
        session.commit()
        candidate = session.query(Candidate).filter_by(source_id="no-email").one()

        enroll_candidates(
            session, sequence=outreach_fixture["sequence"], candidate_ids=[candidate.id]
        )
        session.commit()
        summary = run_due(session, org_id=account["org_id"])
        session.commit()

        assert summary.failed == 1 and not mailbox.outbox
        enrollment = session.query(Enrollment).filter_by(candidate_id=candidate.id).one()
        assert enrollment.status == "stopped"
        assert enrollment.stopped_reason == "missing_email"

    def test_enrolment_is_idempotent(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        candidate_id = outreach_fixture["candidates"][0].id
        enroll_candidates(session, sequence=sequence, candidate_ids=[candidate_id])
        enroll_candidates(session, sequence=sequence, candidate_ids=[candidate_id])
        session.commit()
        assert session.query(Enrollment).count() == 1

    def test_dry_run_sends_nothing(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        enroll_candidates(
            session, sequence=sequence, candidate_ids=[outreach_fixture["candidates"][0].id]
        )
        session.commit()
        summary = run_due(session, org_id=sequence.org_id, dry_run=True)
        session.commit()
        assert summary.processed == 1
        assert not mailbox.outbox
        assert session.query(Message).one().status == "queued"

    def test_daily_cap_is_enforced(self, session, outreach_fixture, mailbox, monkeypatch):
        monkeypatch.setenv("OUTREACH_DAILY_CAP", "1")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()

        sequence = outreach_fixture["sequence"]
        enroll_candidates(
            session,
            sequence=sequence,
            candidate_ids=[c.id for c in outreach_fixture["candidates"]],
        )
        session.commit()
        summary = run_due(session, org_id=sequence.org_id)
        session.commit()

        assert summary.sent == 1
        assert summary.skipped >= 1


class TestSendIdempotency:
    def test_the_same_step_is_not_sent_twice(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        now = utcnow()
        run_due(session, org_id=sequence.org_id, now=now)
        session.commit()
        enrollment = session.query(Enrollment).one()
        enrollment.current_step = 0
        enrollment.status = "active"
        enrollment.next_send_at = now
        session.commit()
        run_due(session, org_id=sequence.org_id, now=now)
        session.commit()
        assert len(mailbox.outbox) == 1
        assert session.query(Message).filter_by(direction="outbound").count() == 1

    def test_send_step_returns_the_existing_row(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        [enrollment] = enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        now = utcnow()
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        first = send_step(
            session,
            enrollment=enrollment,
            sequence=sequence,
            step=step,
            candidate=ada,
            now=now,
        )
        second = send_step(
            session,
            enrollment=enrollment,
            sequence=sequence,
            step=step,
            candidate=ada,
            now=now,
        )
        session.commit()
        assert first.id == second.id
        assert first.status == "sent"
        assert len(mailbox.outbox) == 1


class TestCompliance:
    def test_suppressed_address_is_not_contacted(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        suppress(session, org_id=sequence.org_id, email="ada@example.com", reason="manual")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        summary = run_due(session, org_id=sequence.org_id)
        session.commit()
        assert summary.failed == 1 and not mailbox.outbox
        enrollment = session.query(Enrollment).one()
        assert enrollment.status == "stopped"
        assert enrollment.stopped_reason == "suppressed"

    def test_unsubscribe_reply_adds_to_the_suppression_list(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()
        message = session.query(Message).filter_by(direction="outbound").one()
        record_reply(
            session,
            message=message,
            subject="Re",
            body="Please unsubscribe me from this list.",
        )
        session.commit()
        assert is_suppressed(session, org_id=sequence.org_id, email="ada@example.com")
        assert session.query(Enrollment).one().status == "replied"

    def test_unsubscribe_link_stops_mail(self, client, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()
        message = session.query(Message).filter_by(direction="outbound").one()
        token = signed_tracking_token(message.id)

        response = client.get(f"/t/u/{message.id}?k={token}")
        assert response.status_code == 200
        session.expire_all()
        assert is_suppressed(session, org_id=sequence.org_id, email="ada@example.com")
        assert session.get(Enrollment, message.enrollment_id).status == "replied"

        # A later enrolment of the same person must not send.
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        mailbox.clear()
        run_due(session, org_id=sequence.org_id)
        session.commit()
        assert not mailbox.outbox


class TestReplies:
    def _send_first(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(
            session, sequence=sequence, candidate_ids=[ada.id], project_id=sequence.project_id
        )
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()
        return session.query(Message).filter_by(direction="outbound").one()

    def test_positive_reply_stops_the_sequence(self, session, outreach_fixture, mailbox):
        message = self._send_first(session, outreach_fixture)
        verdict = record_reply(
            session, message=message, subject="Re: role", body="Happy to chat next week!"
        )
        session.commit()

        assert verdict.intent == "interested"
        enrollment = session.query(Enrollment).one()
        assert enrollment.status == "replied"
        assert enrollment.next_send_at is None
        assert session.query(Message).filter_by(direction="inbound").count() == 1

    def test_bounce_marks_the_message_and_enrollment(
        self, session, outreach_fixture, mailbox
    ):
        message = self._send_first(session, outreach_fixture)
        record_reply(
            session,
            message=message,
            subject="Undeliverable",
            body="Delivery Status Notification (Failure)",
        )
        session.commit()
        assert message.status == "bounced"
        assert session.query(Enrollment).one().status == "bounced"

    def test_out_of_office_keeps_the_sequence_alive(
        self, session, outreach_fixture, mailbox
    ):
        message = self._send_first(session, outreach_fixture)
        record_reply(session, message=message, subject="Auto", body="I am out of office")
        session.commit()
        assert session.query(Enrollment).one().status == "active"

    def test_reply_advances_the_pipeline_stage(self, session, outreach_fixture, mailbox):
        from juicebox.models import ProjectCandidate

        project = outreach_fixture["project"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        session.add(
            ProjectCandidate(project_id=project.id, candidate_id=ada.id, stage="sourced")
        )
        session.commit()

        message = self._send_first(session, outreach_fixture)
        entry = session.query(ProjectCandidate).one()
        assert entry.stage == "contacted"

        record_reply(session, message=message, subject="Re", body="Sounds interesting")
        session.commit()
        session.refresh(entry)
        assert entry.stage == "replied"

    def test_inbound_matching_by_address(self, session, outreach_fixture, mailbox):
        message = self._send_first(session, outreach_fixture)
        found = find_message_for_reply(
            session,
            org_id=message.org_id,
            candidate_email="ada@example.com",
            provider_message_id=None,
        )
        assert found is not None and found.id == message.id


class TestTracking:
    def test_open_and_click_are_recorded(self, client, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()

        message = session.query(Message).filter_by(direction="outbound").one()
        token = signed_tracking_token(message.id)

        assert client.get(f"/t/o/{message.id}.png?k={token}").status_code == 200
        response = client.get(
            f"/t/c/{message.id}?k={token}&u=https%3A%2F%2Fexample.com%2Fjob",
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == "https://example.com/job"

        session.expire_all()
        message = session.get(Message, message.id)
        assert message.opened_at is not None and message.clicked_at is not None

    def test_a_forged_token_does_not_record(self, client, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        enroll_candidates(
            session,
            sequence=sequence,
            candidate_ids=[outreach_fixture["candidates"][0].id],
        )
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()
        message = session.query(Message).filter_by(direction="outbound").one()

        assert client.get(f"/t/o/{message.id}.png?k=forged").status_code == 200
        session.expire_all()
        assert session.get(Message, message.id).opened_at is None
