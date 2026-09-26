"""Policy refusals and the typed action schema. No browser, no API key."""

from __future__ import annotations

from juicebox.applications.ats.detect import ApplyTarget
from juicebox.applications.submit.base import SubmissionContext
from juicebox.applications.submit.browser.agent.actions import parse_action
from juicebox.applications.submit.browser.agent.observe import Control, Observation
from juicebox.applications.submit.browser.agent.policy import (
    PAGE_APPLICATION,
    PAGE_CAPTCHA,
    PAGE_LOGIN,
    PAGE_RECEIPT,
    PAGE_SELF_ID,
    check,
    page_class,
    resolve_token,
)


def _ctx(**kwargs) -> SubmissionContext:
    base = dict(
        application_id="app-1",
        org_id="org-1",
        candidate_id="cand-1",
        target=ApplyTarget(vendor="greenhouse", apply_url="https://boards.greenhouse.io/x/jobs/1"),
        full_name="Ada Okonkwo",
        first_name="Ada",
        last_name="Okonkwo",
        from_address="ada@example.com",
        phone="+14155550123",
        linkedin_url="https://linkedin.com/in/ada",
        location="SF",
        answers=[],
        posting_url="https://boards.greenhouse.io/x/jobs/1",
    )
    base.update(kwargs)
    return SubmissionContext(**base)


def _obs(*, url="https://boards.greenhouse.io/x/jobs/1", title="Apply", body="", controls=()):
    return Observation(url=url, title=title, body_text=body, controls=tuple(controls))


def _app_controls():
    return (
        Control(id="c001", label="First Name", field_type="text", name="first_name"),
        Control(id="c002", label="Email", field_type="email", name="email"),
        Control(id="c003", label="Resume", field_type="file", name="resume"),
        Control(id="c004", label="Submit Application", field_type="submit", role="submit"),
        Control(id="c005", label="Next", field_type="button", role="button"),
    )


class TestParseAction:
    def test_unknown_type_is_nothing(self):
        assert parse_action({"type": "navigate"}) is None
        assert parse_action(None) is None
        assert parse_action("fill") is None


class TestPageClass:
    def test_login_from_password_field(self):
        obs = _obs(controls=(Control(id="c001", label="Password", field_type="password"),))
        assert page_class(obs) == PAGE_LOGIN

    def test_captcha(self):
        obs = _obs(body="please complete the recaptcha")
        assert page_class(obs) == PAGE_CAPTCHA

    def test_required_self_id(self):
        obs = _obs(
            controls=(
                Control(id="c001", label="Gender", field_type="select", required=True),
            )
        )
        assert page_class(obs) == PAGE_SELF_ID

    def test_optional_self_id_is_not_a_handoff_page(self):
        obs = _obs(
            controls=(
                *_app_controls(),
                Control(id="c099", label="Gender (optional)", field_type="select", required=False),
            )
        )
        assert page_class(obs) == PAGE_APPLICATION

    def test_receipt(self):
        obs = _obs(body="Thank you for applying. We have received your application.")
        assert page_class(obs) == PAGE_RECEIPT


class TestTokenResolve:
    def test_invented_string_is_refused(self):
        _, _, err = resolve_token("Ada Okonkwo", _ctx())
        assert "invented" in err

    def test_unknown_token_is_refused(self):
        _, _, err = resolve_token("candidate.ssn", _ctx())
        assert "unknown token" in err

    def test_missing_answer_is_refused(self):
        _, _, err = resolve_token("answer.q_abc", _ctx())
        assert "no approved answer" in err

    def test_policy_decline_needs_a_control_with_that_option(self):
        _, _, err = resolve_token("policy.decline", _ctx())
        assert "no decline option" in err
        control = Control(
            id="c099",
            label="Gender",
            field_type="select",
            options=("Male", "Prefer not to answer"),
        )
        value, _, err = resolve_token("policy.decline", _ctx(), control=control)
        assert err == ""
        assert value == "Prefer not to answer"

    def test_doc_token_needs_a_path(self):
        _, _, err = resolve_token("doc.resume", _ctx())
        assert "no resume" in err
        _, path, err = resolve_token("doc.resume", _ctx(), resume_path="/tmp/r.pdf")
        assert path == "/tmp/r.pdf" and err == ""


class TestPolicyCheck:
    def test_unknown_control_id(self):
        action = parse_action({"type": "fill", "control_id": "nope", "value_token": "candidate.email"})
        decision = check(
            action, obs=_obs(controls=_app_controls()), ctx=_ctx(),
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed
        assert "unknown control" in decision.reason

    def test_password_control_is_forbidden(self):
        obs = _obs(
            url="https://boards.greenhouse.io/x",
            controls=(
                *_app_controls(),
                Control(id="c010", label="Password", field_type="password", name="password"),
            ),
        )
        # Password on the page is a login page, which is a hand-off.
        action = parse_action({"type": "fill", "control_id": "c010", "value_token": "candidate.email"})
        decision = check(
            action, obs=obs, ctx=_ctx(), allow_submit=True,
            start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed
        assert "hand-off" in decision.reason or "login" in decision.reason

    def test_off_origin_is_refused(self):
        action = parse_action({"type": "click_next", "control_id": "c005"})
        decision = check(
            action,
            obs=_obs(url="https://evil.example/x", controls=_app_controls()),
            ctx=_ctx(),
            allow_submit=True,
            start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed
        assert "off-origin" in decision.reason

    def test_final_submit_disabled_in_preflight(self):
        action = parse_action({"type": "final_submit", "control_id": "c004"})
        decision = check(
            action, obs=_obs(controls=_app_controls()), ctx=_ctx(),
            allow_submit=False, start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed

    def test_click_next_on_submit_control_is_refused(self):
        action = parse_action({"type": "click_next", "control_id": "c004"})
        decision = check(
            action, obs=_obs(controls=_app_controls()), ctx=_ctx(),
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed
        assert "final_submit" in decision.reason

    def test_final_submit_blocked_by_required_blank(self):
        ctx = _ctx(
            answers=[{
                "key": "q_hear", "question": "How did you hear about us?",
                "answer": "", "kind": "custom", "form_required": True,
            }]
        )
        action = parse_action({"type": "final_submit", "control_id": "c004"})
        decision = check(
            action, obs=_obs(controls=_app_controls()), ctx=ctx,
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed
        assert "unanswered" in decision.reason

    def test_final_submit_allowed_on_complete_application(self):
        action = parse_action({"type": "final_submit", "control_id": "c004"})
        decision = check(
            action, obs=_obs(controls=_app_controls()), ctx=_ctx(),
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert decision.allowed

    def test_fill_with_candidate_token(self):
        action = parse_action({"type": "fill", "control_id": "c002", "value_token": "candidate.email"})
        decision = check(
            action, obs=_obs(controls=_app_controls()), ctx=_ctx(),
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert decision.allowed
        assert decision.resolved_value == "ada@example.com"

    def test_optional_self_id_is_left_blank(self):
        obs = _obs(
            controls=(
                *_app_controls(),
                Control(id="c099", label="Gender (optional)", field_type="select", required=False),
            )
        )
        action = parse_action({"type": "select", "control_id": "c099", "value_token": "candidate.email"})
        decision = check(
            action, obs=obs, ctx=_ctx(),
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed
        assert "forbidden" in decision.reason

    def test_optional_self_id_accepts_policy_decline(self):
        obs = _obs(
            controls=(
                *_app_controls(),
                Control(
                    id="c099",
                    label="Gender (optional)",
                    field_type="select",
                    required=False,
                    options=("Male", "Female", "Prefer not to answer"),
                ),
            )
        )
        action = parse_action({"type": "select", "control_id": "c099", "value_token": "policy.decline"})
        decision = check(
            action, obs=obs, ctx=_ctx(),
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert decision.allowed
        assert decision.resolved_value == "Prefer not to answer"

    def test_click_next_on_a_self_id_radio_is_forbidden_unless_decline(self):
        obs = _obs(
            controls=(
                *_app_controls(),
                Control(id="c080", label="Male", field_type="radio", name="gender"),
                Control(id="c081", label="Prefer not to answer", field_type="radio", name="gender"),
            )
        )
        refused = check(
            parse_action({"type": "click_next", "control_id": "c080"}),
            obs=obs, ctx=_ctx(), allow_submit=True,
            start_origin="https://boards.greenhouse.io",
        )
        assert not refused.allowed
        allowed = check(
            parse_action({"type": "click_next", "control_id": "c081"}),
            obs=obs, ctx=_ctx(), allow_submit=True,
            start_origin="https://boards.greenhouse.io",
        )
        assert allowed.allowed

    def test_required_self_id_with_a_decline_option_is_not_a_handoff_page(self):
        obs = _obs(
            controls=(
                *_app_controls(),
                Control(
                    id="c099",
                    label="Gender",
                    field_type="select",
                    required=True,
                    options=("Male", "Female", "Decline to self-identify"),
                ),
            )
        )
        assert page_class(obs) == PAGE_APPLICATION

    def test_captcha_field_is_a_handoff_page(self):
        obs = _obs(
            controls=(
                *_app_controls(),
                Control(id="c020", label="reCAPTCHA", field_type="text", name="g-recaptcha-response"),
            )
        )
        action = parse_action({"type": "fill", "control_id": "c002", "value_token": "candidate.email"})
        decision = check(
            action, obs=obs, ctx=_ctx(),
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed
        assert "hand-off" in decision.reason or "captcha" in decision.reason

    def test_payment_page_is_a_handoff(self):
        obs = _obs(body="Please enter your credit card number to continue")
        action = parse_action({"type": "fill", "control_id": "c002", "value_token": "candidate.email"})
        decision = check(
            action, obs=obs, ctx=_ctx(),
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed

    def test_signature_blocks_final_submit(self):
        obs = _obs(
            controls=(
                *_app_controls(),
                Control(id="c030", label="I agree and electronically sign", field_type="checkbox"),
            )
        )
        action = parse_action({"type": "final_submit", "control_id": "c004"})
        decision = check(
            action, obs=obs, ctx=_ctx(),
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed
        assert "signature" in decision.reason

    def test_attestation_blank_blocks_final_submit(self):
        ctx = _ctx(
            answers=[{
                "key": "q_vet", "question": "Veteran status",
                "answer": "", "kind": "attestation", "form_required": True,
            }]
        )
        action = parse_action({"type": "final_submit", "control_id": "c004"})
        decision = check(
            action, obs=_obs(controls=_app_controls()), ctx=ctx,
            allow_submit=True, start_origin="https://boards.greenhouse.io",
        )
        assert not decision.allowed
        assert "unanswered" in decision.reason
