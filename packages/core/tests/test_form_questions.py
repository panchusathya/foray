"""A form's own questions: discovered, classified, answered by the right hand.

The stakes are uneven across kinds, and the tests follow the stakes. A custom
question misfiled as identity is a blank on a form; an attestation misfiled as
custom is machinery answering for somebody about their disability status. So
the classifier tests lean hard on the attestation edge, and the submit-time
tests prove a required blank stops the run before a browser ever opens.
"""

from __future__ import annotations

import pytest

from juicebox.applications.submit.base import SubmissionContext
from juicebox.applications.submit.browser.hosted_form import (
    discovered_specs,
    unanswerable_questions,
)
from juicebox.applications.submit.browser.questions import (
    KIND_ATTESTATION,
    KIND_CUSTOM,
    KIND_IDENTITY,
    KIND_STANDARD,
    classify,
    question_key,
)


class TestClassify:
    @pytest.mark.parametrize(
        "label",
        [
            "Are you a veteran?",
            "Veteran Status",
            "Disability status (voluntary self-identification)",
            "Gender",
            "Race/Ethnicity",
            "Are you Hispanic or Latino?",
            "Have you previously worked for Voice Capital?",
            "Have you ever been convicted of a felony?",
            "Are you at least 18 years of age?",
            "Do you consent to a background check?",
            "Sexual orientation (optional)",
        ],
    )
    def test_attestations(self, label):
        assert classify(label)[0] == KIND_ATTESTATION

    @pytest.mark.parametrize(
        ("label", "key"),
        [
            ("What are your salary expectations?", "salary_expectation"),
            ("Desired compensation", "salary_expectation"),
            ("Notice period", "notice_period"),
            ("Earliest start date", "notice_period"),
            ("Will you now or in the future require sponsorship?", "work_authorization"),
            ("Are you authorized to work in the United States?", "work_authorization"),
            ("Why do you want to work at Baseten?", "why_company"),
        ],
    )
    def test_standard(self, label, key):
        kind, standard_key = classify(label)
        assert kind == KIND_STANDARD
        assert standard_key == key

    @pytest.mark.parametrize(
        "label",
        ["First Name", "Email", "Phone", "Resume/CV", "LinkedIn Profile", "Current Location"],
    )
    def test_identity(self, label):
        assert classify(label)[0] == KIND_IDENTITY

    @pytest.mark.parametrize(
        "label",
        [
            "How did you hear about us?",
            "Describe a system you designed end to end.",
            "What excites you most about evals?",
            "GitHub or portfolio URL",
        ],
    )
    def test_custom(self, label):
        assert classify(label)[0] == KIND_CUSTOM

    def test_attestation_wins_over_everything(self):
        """"Why" appears in the label, and it is still a self-ID question."""
        kind, _ = classify("Why we ask: veteran status helps us report EEO data")
        assert kind == KIND_ATTESTATION

    def test_an_empty_label_is_custom_not_a_crash(self):
        assert classify("")[0] == KIND_CUSTOM


class TestQuestionKey:
    def test_stable_across_whitespace_and_case(self):
        assert question_key("Why  Baseten?") == question_key("why baseten?")

    def test_distinct_questions_get_distinct_keys(self):
        assert question_key("Why Baseten?") != question_key("Why Parasail?")


def _entry(**kwargs) -> dict:
    base = {
        "key": "q_abc",
        "question": "How did you hear about us?",
        "answer": "",
        "kind": "custom",
        "field_type": "text",
        "selector": "#q_abc",
        "form_required": False,
    }
    base.update(kwargs)
    return base


def _ctx(answers: list[dict]) -> SubmissionContext:
    return SubmissionContext(
        application_id="app_x",
        org_id="org_x",
        candidate_id="cand_x",
        target=None,
        full_name="Ada Okonkwo",
        first_name="Ada",
        last_name="Okonkwo",
        from_address="anna+tok@goforay.io",
        reply_to_address="ada@example.com",
        phone="",
        linkedin_url="",
        location="",
        resume=None,
        cover_letter=None,
        answers=answers,
        company="Acme",
        title="Analyst",
        posting_url="",
    )


class TestUnanswerableQuestions:
    def test_a_required_custom_blank_stops_the_run(self):
        ctx = _ctx([_entry(form_required=True)])
        assert unanswerable_questions(ctx) == ["How did you hear about us?"]

    def test_an_optional_blank_does_not(self):
        assert unanswerable_questions(_ctx([_entry()])) == []

    def test_an_answered_required_question_does_not(self):
        ctx = _ctx([_entry(form_required=True, answer="A friend at the company.")])
        assert unanswerable_questions(ctx) == []

    def test_an_optional_attestation_blank_is_a_legitimate_submission(self):
        """Blank IS the answer: declining to self-identify."""
        ctx = _ctx([_entry(kind="attestation", question="Veteran status")])
        assert unanswerable_questions(ctx) == []

    def test_a_required_attestation_blank_stops_the_run(self):
        ctx = _ctx([_entry(kind="attestation", question="Veteran status", form_required=True)])
        assert unanswerable_questions(ctx) == ["Veteran status"]

    def test_standard_answers_are_not_its_business(self):
        """The vendor spec's required set already covers those."""
        ctx = _ctx([{"key": "salary_expectation", "answer": "", "question": "Comp?"}])
        assert unanswerable_questions(ctx) == []


class TestDiscoveredSpecs:
    def test_an_answered_custom_question_becomes_a_fillable_spec(self):
        specs = discovered_specs(_ctx([_entry(answer="A friend at the company.")]))
        assert len(specs) == 1
        assert specs[0].selectors == ("#q_abc",)
        assert specs[0].value == "A friend at the company."
        assert specs[0].kind == "text"

    def test_a_candidate_answered_attestation_is_filled_verbatim(self):
        """Their portal edit is the one hand allowed to write these."""
        specs = discovered_specs(
            _ctx([_entry(kind="attestation", answer="I am not a veteran",
                         field_type="radio", selector="input[type=radio][name=vet]")])
        )
        assert len(specs) == 1
        assert specs[0].kind == "radio"

    def test_field_types_map_to_driver_kinds(self):
        specs = discovered_specs(
            _ctx([
                _entry(key="a", selector="#a", field_type="textarea", answer="x"),
                _entry(key="b", selector="#b", field_type="select", answer="Yes"),
                _entry(key="c", selector="#c", field_type="checkbox", answer="yes"),
            ])
        )
        assert [s.kind for s in specs] == ["text", "select", "check"]

    def test_a_custom_file_upload_is_never_ours_to_fill(self):
        specs = discovered_specs(_ctx([_entry(field_type="file", answer="whatever")]))
        assert specs == []

    def test_no_selector_means_no_spec(self):
        assert discovered_specs(_ctx([_entry(selector="", answer="x")])) == []

    def test_standard_entries_are_left_to_the_vendor_spec(self):
        specs = discovered_specs(
            _ctx([{"key": "why_company", "answer": "Because.", "selector": "#w"}])
        )
        assert specs == []


class TestAugment:
    """The merge is pure given a stubbed discovery; the browser is not needed."""

    def _augment(self, monkeypatch, session, discovered, answers, available=True):
        from types import SimpleNamespace

        from juicebox.applications import questions as questions_mod
        from juicebox.applications.ats import detect
        from juicebox.applications.submit.browser import driver
        from juicebox.applications.submit.browser import questions as browser_q

        monkeypatch.setattr(
            detect,
            "target_of",
            lambda posting: SimpleNamespace(
                vendor="ashby",
                apply_url="https://jobs.ashbyhq.com/x/y",
                tenant="x",
                external_job_id="y",
                tier=2,
            ),
        )
        monkeypatch.setattr(driver, "available", lambda: (available, "" if available else "no playwright"))
        monkeypatch.setattr(browser_q, "discover", lambda url, vendor="": discovered)

        application = SimpleNamespace(id="app_x", form_answers=answers)
        posting = SimpleNamespace(id="jpost_x")
        return questions_mod.augment_with_form_questions(
            session, application=application, posting=posting, answers=answers
        )

    def _question(self, **kwargs):
        from juicebox.applications.submit.browser.questions import FormQuestion

        base = dict(
            key=question_key(kwargs.get("label", "Q")),
            label="Q",
            kind=KIND_CUSTOM,
            field_type="text",
            selector="#q",
            required=False,
            options=(),
            standard_key="",
        )
        base.update(kwargs)
        return FormQuestion(**base)

    def test_custom_and_attestation_questions_are_added(self, monkeypatch, session):
        merged = self._augment(
            monkeypatch,
            session,
            [
                self._question(label="How did you hear about us?"),
                self._question(label="Veteran status", kind=KIND_ATTESTATION,
                               field_type="radio", options=("Yes", "No")),
            ],
            [{"key": "why_company", "question": "Why?", "answer": ""}],
        )
        kinds = {item.get("kind") for item in merged if item.get("origin") == "form"}
        assert kinds == {"custom", "attestation"}
        assert len(merged) == 3

    def test_identity_and_standard_discoveries_add_nothing(self, monkeypatch, session):
        merged = self._augment(
            monkeypatch,
            session,
            [
                self._question(label="Email", kind=KIND_IDENTITY),
                self._question(label="Salary?", kind=KIND_STANDARD,
                               standard_key="salary_expectation", key="salary_expectation"),
            ],
            [{"key": "salary_expectation", "question": "Comp?", "answer": "250k"}],
        )
        assert len(merged) == 1

    def test_a_required_standard_question_stamps_the_existing_entry(self, monkeypatch, session):
        merged = self._augment(
            monkeypatch,
            session,
            [
                self._question(label="Salary?", kind=KIND_STANDARD, required=True,
                               standard_key="salary_expectation", key="salary_expectation"),
            ],
            [{"key": "salary_expectation", "question": "Comp?", "answer": ""}],
        )
        assert merged[0]["form_required"] is True

    def test_rediscovery_keeps_the_candidates_answer(self, monkeypatch, session):
        """Their words survive a package rebuild; the DOM facts refresh."""
        key = question_key("How did you hear about us?")
        merged = self._augment(
            monkeypatch,
            session,
            [self._question(label="How did you hear about us?", key=key,
                            selector="#renamed", required=True)],
            [{"key": key, "question": "How did you hear about us?",
              "answer": "A friend.", "edited": True, "kind": "custom", "selector": "#old"}],
        )
        entry = next(item for item in merged if item["key"] == key)
        assert entry["answer"] == "A friend."
        assert entry["selector"] == "#renamed"
        assert entry["form_required"] is True
        assert len(merged) == 1

    def test_no_browser_degrades_to_the_standard_set(self, monkeypatch, session):
        answers = [{"key": "why_company", "question": "Why?", "answer": ""}]
        merged = self._augment(monkeypatch, session, [], answers, available=False)
        assert merged is answers

    def test_discovery_failure_is_not_a_package_failure(self, monkeypatch, session):
        from types import SimpleNamespace

        from juicebox.applications import questions as questions_mod
        from juicebox.applications.ats import detect
        from juicebox.applications.submit.browser import driver
        from juicebox.applications.submit.browser import questions as browser_q

        monkeypatch.setattr(
            detect, "target_of",
            lambda posting: SimpleNamespace(vendor="ashby", apply_url="https://x", tenant="", external_job_id="", tier=2),
        )
        monkeypatch.setattr(driver, "available", lambda: (True, ""))

        def boom(url, vendor=""):
            raise RuntimeError("page timed out")

        monkeypatch.setattr(browser_q, "discover", boom)
        answers = [{"key": "why_company", "question": "Why?", "answer": ""}]
        merged = questions_mod.augment_with_form_questions(
            session,
            application=SimpleNamespace(id="app_x", form_answers=answers),
            posting=SimpleNamespace(id="jpost_x"),
            answers=answers,
        )
        assert merged is answers


from juicebox.models import Application, Candidate, JobPosting
from juicebox.portal.links import mint_session_token
from juicebox.repository import upsert_candidates


@pytest.fixture
def candidate(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    row = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
    row.emails = ["ada@example.com"]
    session.flush()
    return row


def _headers(account, candidate) -> dict:
    token = mint_session_token(org_id=account["org_id"], candidate_id=candidate.id)
    return {"Authorization": f"Bearer {token}"}


def _application(session, account, candidate, *, status="prepared", answers=None) -> Application:
    posting = JobPosting(
        org_id=account["org_id"], company="Acme", title="Analyst",
        source="test", source_id=f"acme-{status}",
    )
    session.add(posting)
    session.flush()
    row = Application(
        org_id=account["org_id"],
        candidate_id=candidate.id,
        job_posting_id=posting.id,
        status=status,
        form_answers=answers
        if answers is not None
        else [
            {"key": "why_company", "question": "Why Acme?", "answer": "",
             "generated_by": "template", "edited": False},
            {"key": "q_vet", "question": "Veteran status", "answer": "",
             "kind": "attestation", "field_type": "radio", "selector": "input[name=v]"},
        ],
    )
    session.add(row)
    session.commit()
    return row


class TestAnswerEditing:
    """The endpoint the follow-up text points at, and the only door for
    attestations. Candidate-scoped like every portal handler."""

    def test_the_candidate_answers_their_own_question(
        self, client, session, account, candidate
    ):
        application = _application(session, account, candidate)
        response = client.patch(
            f"/v1/portal/applications/{application.id}/answers",
            headers=_headers(account, candidate),
            json={"answers": [{"key": "why_company", "answer": "I know the fund."}]},
        )
        assert response.status_code == 200
        entry = next(a for a in response.json()["form_answers"] if a["key"] == "why_company")
        assert entry["answer"] == "I know the fund."
        assert entry["edited"] is True
        assert entry["generated_by"] == "candidate"

    def test_an_attestation_takes_only_their_hand(self, client, session, account, candidate):
        """Filling it here is the point; nothing else may."""
        application = _application(session, account, candidate)
        response = client.patch(
            f"/v1/portal/applications/{application.id}/answers",
            headers=_headers(account, candidate),
            json={"answers": [{"key": "q_vet", "answer": "I am not a veteran"}]},
        )
        assert response.status_code == 200
        entry = next(a for a in response.json()["form_answers"] if a["key"] == "q_vet")
        assert entry["provenance"] == "candidate:portal"

    def test_a_key_the_form_never_asked_is_refused(self, client, session, account, candidate):
        """An unasked answer would go on the wire at submit time."""
        application = _application(session, account, candidate)
        response = client.patch(
            f"/v1/portal/applications/{application.id}/answers",
            headers=_headers(account, candidate),
            json={"answers": [{"key": "made_up", "answer": "x"}]},
        )
        assert response.status_code == 422
        assert "made_up" in response.json()["detail"]

    def test_answers_lock_once_submission_starts(self, client, session, account, candidate):
        application = _application(session, account, candidate, status="submitting")
        response = client.patch(
            f"/v1/portal/applications/{application.id}/answers",
            headers=_headers(account, candidate),
            json={"answers": [{"key": "why_company", "answer": "late"}]},
        )
        assert response.status_code == 409

    def test_another_candidates_application_is_a_404(
        self, client, session, account, candidate
    ):
        other = Candidate(
            org_id=account["org_id"], full_name="Someone Else",
            source="manual", source_id="other-answers",
        )
        session.add(other)
        session.flush()
        application = _application(session, account, other)
        response = client.patch(
            f"/v1/portal/applications/{application.id}/answers",
            headers=_headers(account, candidate),
            json={"answers": [{"key": "why_company", "answer": "not mine"}]},
        )
        assert response.status_code == 404

    def test_saving_nothing_is_a_422(self, client, session, account, candidate):
        application = _application(session, account, candidate)
        response = client.patch(
            f"/v1/portal/applications/{application.id}/answers",
            headers=_headers(account, candidate),
            json={"answers": []},
        )
        assert response.status_code == 422

    def test_their_edit_survives_the_preference_refill(
        self, client, session, account, candidate
    ):
        """`refill_from_preference` runs on every read; `edited` shields it."""
        application = _application(session, account, candidate)
        client.patch(
            f"/v1/portal/applications/{application.id}/answers",
            headers=_headers(account, candidate),
            json={"answers": [{"key": "why_company", "answer": "Mine."}]},
        )
        read_back = client.get(
            f"/v1/portal/applications/{application.id}",
            headers=_headers(account, candidate),
        )
        entry = next(a for a in read_back.json()["form_answers"] if a["key"] == "why_company")
        assert entry["answer"] == "Mine."


class TestFollowupNamesTheFormsQuestions:
    class _Posting:
        company = "Baseten"
        title = "Founding Engineer"

    def _app(self, extra):
        from types import SimpleNamespace

        answers = [
            {"key": "salary_expectation", "question": "Comp?", "answer": "USD 250,000"},
            {"key": "notice_period", "question": "Start?", "answer": "Immediately"},
            {"key": "work_authorization", "question": "Auth?", "answer": "US citizen"},
            {"key": "why_company", "question": "Why?", "answer": "Because."},
            *extra,
        ]
        return SimpleNamespace(form_answers=answers)

    def test_a_required_custom_question_is_quoted(self):
        from juicebox.applications.followups import followup_message

        body = followup_message(
            self._app([_entry(question="How did you hear about us?", form_required=True)]),
            self._Posting(),
        )
        assert '"How did you hear about us?"' in body
        assert "answer in the app" in body

    def test_attestations_are_counted_never_quoted(self):
        """Race and disability do not belong in an SMS, even as questions."""
        from juicebox.applications.followups import followup_message

        body = followup_message(
            self._app([
                _entry(key="q_v", question="Veteran status", kind="attestation"),
                _entry(key="q_d", question="Disability status", kind="attestation"),
            ]),
            self._Posting(),
        )
        assert "Veteran" not in body
        assert "Disability" not in body
        assert "2 self-ID questions" in body
        assert "i never fill them in" in body

    def test_a_flood_of_custom_questions_collapses_to_a_count(self):
        from juicebox.applications.followups import (
            MAX_NAMED_FORM_QUESTIONS,
            followup_message,
        )

        extra = [
            _entry(key=f"q_{i}", question=f"Question number {i}?") for i in range(5)
        ]
        body = followup_message(self._app(extra), self._Posting())
        assert body.count("their form asks") == MAX_NAMED_FORM_QUESTIONS
        assert "plus 3 more of their questions" in body

    def test_required_questions_are_named_before_optional_ones(self):
        from juicebox.applications.followups import followup_message

        body = followup_message(
            self._app([
                _entry(key="q_opt", question="Optional trivia?"),
                _entry(key="q_req", question="Required essay?", form_required=True),
            ]),
            self._Posting(),
        )
        assert body.index("Required essay") < body.index("Optional trivia")

    def test_everything_answered_is_still_silent(self):
        from juicebox.applications.followups import followup_message

        assert followup_message(self._app([]), self._Posting()) == ""
