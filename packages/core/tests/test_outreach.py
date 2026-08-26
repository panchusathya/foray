from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from juicebox.models import (
    Candidate,
    Enrollment,
    Message,
    ProjectCandidate,
    Suppression,
    utcnow,
)
from juicebox.outreach import templates
from juicebox.outreach.personalize import personalize
from juicebox.outreach.replies import heuristic_classify
from juicebox.outreach.sequencer import (
    _sendable_address,
    enroll_candidates,
    find_message_for_reply,
    preview_step,
    record_reply,
    run_due,
    schedule_step_at,
    send_step,
)
from juicebox.outreach.suppressions import is_suppressed, suppress
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

    def test_unknown_variables_are_dropped_from_generated_copy(self):
        """A model asked for a signature writes {{your_name}} whatever the prompt
        says, and render() would collapse it to a blank in a real email."""
        from juicebox.llm.prompts import SEQUENCE_TEMPLATE_VARIABLES

        cleaned = templates.keep_known_variables(
            "Hi {{first_name}},\n\nBest,\n{{your_name}}", SEQUENCE_TEMPLATE_VARIABLES
        )
        assert "{{first_name}}" in cleaned
        assert "your_name" not in cleaned

    def test_an_unknown_variable_with_a_fallback_keeps_the_fallback_text(self):
        cleaned = templates.keep_known_variables("at {{employer | your team}}", ["first_name"])
        assert cleaned == "at your team"

    def test_keeping_known_variables_leaves_a_clean_template_alone(self):
        template = "Hi {{first_name}}, about {{role_title}}."
        assert templates.keep_known_variables(template, ["first_name", "role_title"]) == template

    def test_html_escapes_and_paragraphs(self):
        html = templates.to_html("Hi <script>\n\nSecond para")
        assert "&lt;script&gt;" in html
        assert html.count("<p>") == 2

    def test_html_templates_keep_formatting_and_drop_scripts(self):
        html = templates.to_html(
            '<p>Hi {{ first_name }}, <strong>please</strong> read '
            '<a href="https://jobs.example.com/role">this</a>'
            '<script>alert(1)</script></p>'
        )
        assert "<strong>please</strong>" in html
        assert '<a href="https://jobs.example.com/role">this</a>' in html
        assert "{{ first_name }}" in html
        assert "<script>" not in html
        assert "alert" not in html

    def test_html_templates_unwrap_unsafe_links(self):
        html = templates.to_html('<p><a href="javascript:alert(1)">click</a></p>')
        assert "javascript" not in html.lower()
        assert html == "<p>click</p>"

    def test_variables_still_substitute_inside_html(self):
        rendered = templates.render(
            "<p>Hi {{ first_name }}, <em>{{ role_title | the role }}</em></p>",
            {"first_name": "Ada"},
        )
        html = templates.to_html(rendered)
        assert html == "<p>Hi Ada, <em>the role</em></p>"

    def test_to_text_strips_tags_and_keeps_plain_text(self):
        assert templates.to_text("<p>Hi <strong>Ada</strong></p><p>Bye</p>") == "Hi Ada\n\nBye"
        assert templates.to_text("Hi Ada,\n\nBye") == "Hi Ada,\n\nBye"

    def test_sanitize_preview_leaves_plain_text_alone(self):
        assert templates.sanitize_preview("Hi Ada") == "Hi Ada"
        assert "<script>" not in templates.sanitize_preview("<p>Hi<script>alert(1)</script></p>")

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


class TestSendNowOverride:
    """A recruiter adding one person late in the day expects mail now, not a
    silent roll to tomorrow's send_at_time — send_now makes that explicit."""

    def _sequence_with_morning_slot(self, session, fixture):
        sequence = fixture["sequence"]
        steps = sorted(sequence.steps, key=lambda s: s.step_order)
        steps[0].send_at_time = "09:00"
        steps[0].send_timezone = "America/Los_Angeles"
        session.commit()
        return sequence

    def test_default_enrolment_waits_for_the_scheduled_slot(self, session, outreach_fixture):
        sequence = self._sequence_with_morning_slot(session, outreach_fixture)
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enrollment = enroll_candidates(
            session, sequence=sequence, candidate_ids=[ada.id]
        )[0]
        # Snapped to a 09:00 Pacific slot, not "now".
        assert enrollment.next_send_at.minute == 0

    def test_send_now_makes_the_first_step_due_immediately(
        self, session, outreach_fixture, mailbox
    ):
        sequence = self._sequence_with_morning_slot(session, outreach_fixture)
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        now = utcnow()
        enrollment = enroll_candidates(
            session, sequence=sequence, candidate_ids=[ada.id], send_now=True, now=now
        )[0]
        assert enrollment.next_send_at == now

        summary = run_due(session, org_id=sequence.org_id, now=now)
        assert summary.sent == 1
        # Later steps keep their configured timing: the override was only for
        # getting the first email out. (SQLite hands back naive datetimes.)
        session.refresh(enrollment)
        next_at = enrollment.next_send_at
        if next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=UTC)
        assert next_at > now

    def test_send_now_does_not_resend_to_existing_enrollments(
        self, session, outreach_fixture, mailbox
    ):
        sequence = self._sequence_with_morning_slot(session, outreach_fixture)
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        first = enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])[0]
        scheduled = first.next_send_at
        again = enroll_candidates(
            session, sequence=sequence, candidate_ids=[ada.id], send_now=True
        )[0]
        # Idempotent enrolment returns the existing row untouched — send_now on
        # a re-enrol must not yank an already-scheduled send earlier.
        assert again.id == first.id
        assert again.next_send_at == scheduled


class TestSendNowEndpoint:
    """The enroll endpoint itself delivers when send_now is set — one bounded
    request, and the response rows reflect the post-send state. The old flow
    (a second unbounded /run over the whole sequence) could outlive the proxy
    timeout while the UI showed Enrolling… forever."""

    def _make_sequence(self, client, headers, steps):
        return client.post(
            "/v1/sequences",
            json={"name": "SendNow", "personalize": False, "steps": steps},
            headers=headers,
        ).json()

    def test_send_now_sends_inside_the_enroll_request(self, client, loaded_org, mailbox):
        headers = loaded_org["headers"]
        sequence = self._make_sequence(
            client,
            headers,
            [
                {
                    "delay_days": 0,
                    "send_at_time": "09:00",
                    "send_timezone": "America/Los_Angeles",
                    "subject_template": "Hi {{first_name}}",
                    "body_template": "Hello {{first_name}}",
                },
                {"delay_days": 2, "subject_template": "Re", "body_template": "Bump"},
            ],
        )
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]

        rows = client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate_id], "send_now": True},
            headers=headers,
        ).json()

        # The email went out during THIS request, and the returned row shows
        # it: step advanced, next_send_at now belongs to the follow-up.
        assert len(mailbox.outbox) == 1
        assert rows[0]["current_step"] == 1
        assert rows[0]["next_send_at"] is not None

    def test_without_send_now_the_enroll_request_sends_nothing(
        self, client, loaded_org, mailbox
    ):
        headers = loaded_org["headers"]
        sequence = self._make_sequence(
            client,
            headers,
            [
                {
                    "delay_days": 0,
                    "send_at_time": "09:00",
                    "send_timezone": "America/Los_Angeles",
                    "subject_template": "Hi {{first_name}}",
                    "body_template": "Hello {{first_name}}",
                }
            ],
        )
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]
        rows = client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate_id]},
            headers=headers,
        ).json()
        assert mailbox.outbox == []
        assert rows[0]["current_step"] == 0

    def test_send_now_on_a_paused_sequence_enrolls_but_does_not_send(
        self, client, loaded_org, mailbox
    ):
        headers = loaded_org["headers"]
        sequence = self._make_sequence(
            client,
            headers,
            [{"delay_days": 0, "subject_template": "Hi", "body_template": "Hello"}],
        )
        client.patch(
            f"/v1/sequences/{sequence['id']}/status?value=paused", headers=headers
        )
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]
        rows = client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate_id], "send_now": True},
            headers=headers,
        ).json()
        assert mailbox.outbox == []
        assert rows[0]["current_step"] == 0


class TestRunEndpointDryRun:
    def test_a_preview_serialises_and_leaves_the_enrollment_alone(
        self, client, loaded_org, mailbox
    ):
        """The endpoint serialises Messages that were never flushed, so every
        column MessageOut requires has to be present without a DB round trip."""
        headers = loaded_org["headers"]
        sequence = client.post(
            "/v1/sequences",
            json={
                "name": "Preview",
                "personalize": False,
                "steps": [
                    {
                        "delay_days": 0,
                        "subject_template": "Hi {{first_name}}",
                        "body_template": "Hello {{first_name}}",
                    },
                    {"delay_days": 2, "subject_template": "Re", "body_template": "Bump"},
                ],
            },
            headers=headers,
        ).json()
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]
        client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate_id]},
            headers=headers,
        )

        response = client.post(
            f"/v1/sequences/{sequence['id']}/run?dry_run=true", headers=headers
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["processed"] == 1
        [preview] = payload["messages"]
        assert preview["subject"] == "Hi Ada"
        assert preview["status"] == "queued"
        assert preview["created_at"]

        assert not mailbox.outbox
        rows = client.get(
            f"/v1/sequences/{sequence['id']}/enrollments", headers=headers
        ).json()
        assert rows[0]["current_step"] == 0
        messages = client.get(
            f"/v1/messages?candidate_id={candidate_id}", headers=headers
        ).json()
        assert messages == []


class TestStepScheduling:
    """schedule_step_at is pure — no DB, just a step's scheduling fields."""

    @staticmethod
    def step(**overrides):
        from juicebox.models import SequenceStep

        defaults = dict(delay_days=0, delay_unit="days", send_at_time="", send_timezone="")
        return SequenceStep(**{**defaults, **overrides})

    def test_plain_delay_is_unchanged(self):
        base = datetime(2026, 1, 12, 15, 0, tzinfo=UTC)  # a Monday
        due = schedule_step_at(self.step(delay_days=3), base=base)
        assert due == base + timedelta(days=3)

    def test_business_days_skip_the_weekend(self):
        friday = datetime(2026, 1, 16, 10, 0, tzinfo=UTC)
        due = schedule_step_at(
            self.step(delay_days=1, delay_unit="business_days"), base=friday
        )
        assert due == datetime(2026, 1, 19, 10, 0, tzinfo=UTC)  # Monday

    def test_send_time_snaps_to_the_delay_day(self):
        base = datetime(2026, 1, 12, 15, 0, tzinfo=UTC)
        due = schedule_step_at(self.step(delay_days=1, send_at_time="09:00"), base=base)
        assert due == datetime(2026, 1, 13, 9, 0, tzinfo=UTC)

    def test_todays_time_already_past_rolls_to_tomorrow(self):
        base = datetime(2026, 1, 12, 15, 0, tzinfo=UTC)
        due = schedule_step_at(self.step(send_at_time="09:00"), base=base)
        assert due == datetime(2026, 1, 13, 9, 0, tzinfo=UTC)

    def test_todays_time_still_ahead_sends_today(self):
        base = datetime(2026, 1, 12, 6, 30, tzinfo=UTC)
        due = schedule_step_at(self.step(send_at_time="09:00"), base=base)
        assert due == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)

    def test_timezone_is_honored(self):
        base = datetime(2026, 1, 12, 8, 0, tzinfo=UTC)
        due = schedule_step_at(
            self.step(delay_days=1, send_at_time="09:00", send_timezone="America/Los_Angeles"),
            base=base,
        )
        assert due == datetime(2026, 1, 13, 17, 0, tzinfo=UTC)  # 09:00 PST

    def test_unknown_timezone_falls_back_to_utc(self):
        base = datetime(2026, 1, 12, 6, 0, tzinfo=UTC)
        due = schedule_step_at(
            self.step(send_at_time="09:00", send_timezone="Mars/Olympus_Mons"), base=base
        )
        assert due == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)

    def test_snapped_business_day_never_lands_on_a_weekend(self):
        friday_evening = datetime(2026, 1, 16, 22, 0, tzinfo=UTC)
        due = schedule_step_at(
            self.step(delay_unit="business_days", send_at_time="09:00"), base=friday_evening
        )
        assert due == datetime(2026, 1, 19, 9, 0, tzinfo=UTC)  # Monday, not Saturday


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
        # Reads like a personal email — no bulk-mail footer or headers.
        assert "unsubscribe" not in mailbox.outbox[0].text_body.lower()
        assert "unsubscribe" not in mailbox.outbox[0].html_body.lower()
        assert "List-Unsubscribe" not in mailbox.outbox[0].headers

        enrollment = session.query(Enrollment).one()
        assert enrollment.current_step == 1
        assert enrollment.status == "active"
        assert enrollment.next_send_at is not None

    def test_html_body_template_sends_formatted_mail(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        steps = sorted(sequence.steps, key=lambda s: s.step_order)
        steps[0].body_template = (
            '<p>Hi {{ first_name }}, see <strong>us</strong> at '
            '<a href="https://jobs.example.com/role">this role</a>.</p>'
            "<p><script>alert(1)</script></p>"
        )
        session.commit()
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()

        email = mailbox.outbox[0]
        assert "<strong>us</strong>" in email.html_body
        assert "this role" in email.html_body
        assert "/t/c/" in email.html_body
        assert "script" not in email.html_body
        assert "alert" not in email.html_body
        assert "<strong>" not in email.text_body
        assert "Hi Ada" in email.text_body
        assert "this role" in email.text_body
        stored = session.query(Message).one()
        assert stored.body == email.text_body
        assert "<strong>" not in stored.body

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

    def test_a_low_reach_score_stops_the_send(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        monkeypatch.setenv("FORAY_MIN_REACH_SCORE", "40")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        ada.emails = ["ada@example.com"]
        ada.raw = {"foray": {"email_reach": {"ada@example.com": 5}}}
        session.commit()
        enroll_candidates(
            session, sequence=outreach_fixture["sequence"], candidate_ids=[ada.id]
        )
        session.commit()
        summary = run_due(session, org_id=outreach_fixture["sequence"].org_id)
        session.commit()
        assert summary.failed == 1 and not mailbox.outbox
        enrollment = session.query(Enrollment).filter_by(candidate_id=ada.id).one()
        assert enrollment.status == "stopped"
        assert enrollment.stopped_reason == "reach_score_below_floor"

    def test_the_floor_skips_to_the_next_address(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        monkeypatch.setenv("FORAY_MIN_REACH_SCORE", "40")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        ada.emails = ["low@gmail.com", "ada@example.com"]
        ada.raw = {
            "foray": {"email_reach": {"low@gmail.com": 5, "ada@example.com": 80}}
        }
        session.commit()
        enroll_candidates(
            session, sequence=outreach_fixture["sequence"], candidate_ids=[ada.id]
        )
        session.commit()
        run_due(session, org_id=outreach_fixture["sequence"].org_id)
        session.commit()
        assert mailbox.outbox[0].to == "ada@example.com"

    def test_an_unscored_address_is_not_blocked_by_the_floor(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        monkeypatch.setenv("FORAY_MIN_REACH_SCORE", "40")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        ada.raw = {}
        session.commit()
        enroll_candidates(
            session, sequence=outreach_fixture["sequence"], candidate_ids=[ada.id]
        )
        session.commit()
        run_due(session, org_id=outreach_fixture["sequence"].org_id)
        session.commit()
        assert mailbox.outbox

    def test_enrolment_is_idempotent(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        candidate_id = outreach_fixture["candidates"][0].id
        enroll_candidates(session, sequence=sequence, candidate_ids=[candidate_id])
        enroll_candidates(session, sequence=sequence, candidate_ids=[candidate_id])
        session.commit()
        assert session.query(Enrollment).count() == 1

    def test_a_repeated_id_in_one_payload_does_not_sink_the_batch(
        self, session, outreach_fixture
    ):
        """Two Enrollments for one candidate trip the unique constraint, and the
        rollback takes everyone else in the request with it."""
        sequence = outreach_fixture["sequence"]
        first, second = outreach_fixture["candidates"][0], outreach_fixture["candidates"][1]
        created = enroll_candidates(
            session,
            sequence=sequence,
            candidate_ids=[first.id, second.id, first.id],
        )
        session.commit()
        assert len(created) == 2
        assert session.query(Enrollment).count() == 2

    def test_dry_run_leaves_no_trace_and_the_step_still_sends(
        self, session, outreach_fixture, mailbox
    ):
        """A preview that writes anything skips the step it previewed.

        The run endpoint is committed by CommittingRoute, so a persisted
        dry-run Message plus the advance it triggers left the enrollment on
        step 2, the person marked contacted, and step 1 never sent.
        """
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        session.add(
            ProjectCandidate(
                project_id=sequence.project_id, candidate_id=ada.id, stage="sourced"
            )
        )
        [enrollment] = enroll_candidates(
            session,
            sequence=sequence,
            candidate_ids=[ada.id],
            project_id=sequence.project_id,
        )
        session.commit()
        due_at = enrollment.next_send_at

        summary = run_due(session, org_id=sequence.org_id, dry_run=True)
        session.commit()

        # The preview still describes the mail that would have gone out...
        assert summary.processed == 1
        [preview] = summary.messages
        assert preview.to_address == "ada@example.com"
        assert preview.subject and preview.body
        assert preview.provider_message_id == "dry-run"

        # ...and nothing else happened at all.
        assert not mailbox.outbox
        assert session.query(Message).count() == 0
        assert enrollment.current_step == 0
        assert enrollment.status == "active"
        assert enrollment.next_send_at == due_at
        assert session.query(ProjectCandidate).one().stage == "sourced"
        assert enrollment.personalization in (None, {})

        # So step 1 is still there to send for real.
        run_due(session, org_id=sequence.org_id)
        session.commit()
        assert len(mailbox.outbox) == 1
        assert enrollment.current_step == 1
        assert session.query(ProjectCandidate).one().stage == "contacted"

    def test_a_dry_run_of_a_suppressed_candidate_does_not_stop_the_enrollment(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        suppress(session, org_id=sequence.org_id, email="ada@example.com")
        [enrollment] = enroll_candidates(
            session, sequence=sequence, candidate_ids=[ada.id]
        )
        session.commit()

        summary = run_due(session, org_id=sequence.org_id, dry_run=True)
        session.commit()

        assert summary.failed == 1
        assert summary.messages[0].error == "suppressed"
        assert session.query(Message).count() == 0
        assert enrollment.status == "active"
        assert enrollment.stopped_reason == ""

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


class TestSuppressions:
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

    def test_opt_out_reply_adds_to_the_do_not_contact_list(
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
            body="Please stop contacting me about roles.",
        )
        session.commit()
        assert is_suppressed(session, org_id=sequence.org_id, email="ada@example.com")
        assert session.query(Enrollment).filter_by(candidate_id=ada.id).first().status == "replied"

        # A later enrolment of the same person must not send.
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        mailbox.clear()
        run_due(session, org_id=sequence.org_id)
        session.commit()
        assert not mailbox.outbox

    def test_opt_out_on_a_hand_sent_thread_is_still_suppressed(
        self, session, outreach_fixture, mailbox
    ):
        """Thread-synced mail has no enrollment, and an opt-out is a fact about
        the person either way -- otherwise enrolling them later mails them."""
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        hand_sent = _thread_synced_outbound(session, org_id=sequence.org_id, candidate=ada)
        session.commit()

        verdict = record_reply(
            session,
            message=hand_sent,
            subject="Re: Quick note",
            body="Please take me off your list.",
        )
        session.commit()

        assert verdict.intent == "unsubscribe"
        assert is_suppressed(session, org_id=sequence.org_id, email="ada@example.com")

        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()
        assert not mailbox.outbox

    def test_bounce_on_a_hand_sent_thread_marks_and_suppresses(
        self, session, outreach_fixture
    ):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        hand_sent = _thread_synced_outbound(session, org_id=sequence.org_id, candidate=ada)
        session.commit()

        record_reply(
            session,
            message=hand_sent,
            subject="Undeliverable",
            body="Delivery Status Notification (Failure)",
        )
        session.commit()

        assert hand_sent.status == "bounced"
        assert is_suppressed(session, org_id=sequence.org_id, email="ada@example.com")

    def test_leaving_do_not_contact_keeps_a_real_opt_out(
        self, session, outreach_fixture, mailbox
    ):
        """set_crm_status may only clear the suppression it wrote. There is one
        row per address, so the row it deletes IS the candidate's opt-out."""
        from juicebox.crm import set_crm_status

        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        suppress(
            session,
            org_id=sequence.org_id,
            email="ada@example.com",
            reason="unsubscribe",
            source="reply",
        )
        set_crm_status(session, ada, "do_not_contact")
        set_crm_status(session, ada, "active")
        session.commit()

        assert is_suppressed(session, org_id=sequence.org_id, email="ada@example.com")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()
        assert not mailbox.outbox

    def test_leaving_do_not_contact_still_clears_its_own_suppression(
        self, session, outreach_fixture, mailbox
    ):
        from juicebox.crm import set_crm_status

        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        set_crm_status(session, ada, "do_not_contact")
        session.commit()
        assert is_suppressed(session, org_id=sequence.org_id, email="ada@example.com")

        set_crm_status(session, ada, "active")
        session.commit()
        assert not is_suppressed(session, org_id=sequence.org_id, email="ada@example.com")

    def test_an_opt_out_outranks_a_hand_set_suppression_on_the_same_address(
        self, session, outreach_fixture
    ):
        """Order matters the other way too: suppress() is idempotent per
        address, so the reply has to upgrade the manual row rather than be
        dropped -- or leaving do_not_contact deletes it."""
        from juicebox.crm import set_crm_status

        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        set_crm_status(session, ada, "do_not_contact")
        hand_sent = _thread_synced_outbound(session, org_id=sequence.org_id, candidate=ada)
        session.commit()

        record_reply(session, message=hand_sent, subject="Re", body="Unsubscribe me please.")
        session.commit()
        assert session.query(Suppression).one().reason == "unsubscribe"

        set_crm_status(session, ada, "active")
        session.commit()
        assert is_suppressed(session, org_id=sequence.org_id, email="ada@example.com")


def _thread_synced_outbound(session, *, org_id: str, candidate) -> Message:
    """An email the recruiter sent by hand from Outlook: mailbox sync stores it
    with ``enrollment_id`` NULL, because no sequence is behind it."""
    message = Message(
        org_id=org_id,
        enrollment_id=None,
        candidate_id=candidate.id,
        direction="outbound",
        source="microsoft",
        from_address="recruiter@acme-talent.com",
        to_address=candidate.emails[0],
        subject="Quick note",
        body="Are you open to a chat?",
        status="sent",
        sent_at=utcnow(),
    )
    session.add(message)
    session.flush()
    return message


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
        inbound = session.query(Message).filter_by(direction="inbound").one()
        assert inbound.from_address == message.to_address
        assert inbound.to_address == message.from_address
        assert inbound.to_address == "talent@acme-talent.com"

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

    def test_inbound_matching_by_rfc_in_reply_to(self, session, outreach_fixture, mailbox):
        message = self._send_first(session, outreach_fixture)
        message.internet_message_id = "<out-1@acme-talent.com>"
        session.flush()
        found = find_message_for_reply(
            session,
            org_id=message.org_id,
            candidate_email=None,
            provider_message_id="<out-1@acme-talent.com>",
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


class TestSendableAddress:
    def test_unscored_passes_the_floor(self):
        cand = SimpleNamespace(emails=["ada@gmail.com"], raw={})
        addr, err = _sendable_address(cand, 40)
        assert addr == "ada@gmail.com" and err is None

    def test_below_floor_is_blocked(self):
        cand = SimpleNamespace(
            emails=["ada@gmail.com"],
            raw={"foray": {"email_reach": {"ada@gmail.com": 5}}},
        )
        addr, err = _sendable_address(cand, 40)
        assert addr == "" and err == "reach_score_below_floor"

    def test_floor_zero_disables_the_gate(self):
        cand = SimpleNamespace(
            emails=["ada@gmail.com"],
            raw={"foray": {"email_reach": {"ada@gmail.com": 0}}},
        )
        addr, err = _sendable_address(cand, 0)
        assert addr == "ada@gmail.com" and err is None

    def test_skips_low_then_picks_the_next(self):
        cand = SimpleNamespace(
            emails=["low@gmail.com", "ok@gmail.com"],
            raw={"foray": {"email_reach": {"low@gmail.com": 5, "ok@gmail.com": 80}}},
        )
        addr, err = _sendable_address(cand, 40)
        assert addr == "ok@gmail.com" and err is None
