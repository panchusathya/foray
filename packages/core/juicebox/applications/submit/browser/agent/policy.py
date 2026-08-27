"""Non-model policy: page class and per-action refusals.

The planner proposes; this module decides. Cheap models fail at bucketing,
so page class is computed from the URL and the control inventory, never
asked of the model. A refusal is ``needs_human``, not a retry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ....eeo import decline_choice, is_decline
from ...base import SubmissionContext
from .actions import (
    ANSWER_PREFIX,
    CANDIDATE_TOKENS,
    CLICK_TYPES,
    DOC_TOKENS,
    FILL_TYPES,
    POLICY_DECLINE,
    Action,
    answer_key,
    is_answer_token,
)
from .observe import PAGE_APPLICATION, PAGE_RECEIPT, Control, Observation

PAGE_LOGIN = "login"
PAGE_CAPTCHA = "captcha"
PAGE_SELF_ID = "self_id"
PAGE_PAYMENT = "payment"
PAGE_UNKNOWN = "unknown"

HANDOFF_PAGES = frozenset({PAGE_LOGIN, PAGE_CAPTCHA, PAGE_SELF_ID, PAGE_PAYMENT, PAGE_UNKNOWN})

_PASSWORD = re.compile(r"password|passwd|passcode", re.I)
_LOGIN = re.compile(r"\b(sign[\s-]?in|log[\s-]?in|sso|single sign)\b", re.I)
_CAPTCHA = re.compile(r"captcha|recaptcha|hcaptcha|turnstile|cf-challenge", re.I)
_SELF_ID = re.compile(
    r"eeoc|self-?identif|gender|race|ethnic|hispanic|veteran|disabilit|"
    r"sexual orientation|transgender|pronoun",
    re.I,
)
_SIGNATURE = re.compile(r"signatur|i agree|i attest|electronically sign", re.I)
_PAYMENT = re.compile(r"credit card|card number|cvv|cvc|billing zip|payment", re.I)
_RECEIPT = re.compile(
    r"thank you for applying|application submitted|application received|"
    r"successfully submitted|we've received your application|"
    r"we have received your application|your application has been submitted",
    re.I,
)
_NEXT = re.compile(r"^\s*(next|continue|save and continue|proceed)\b", re.I)
_SUBMIT = re.compile(r"\b(submit|send application|apply now)\b", re.I)
_CREATE_ACCOUNT = re.compile(r"create (an )?account|register|sign up", re.I)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    resolved_value: str = ""
    resolved_file: str = ""
    page_class: str = ""
    # The control the action actually acts on, when policy resolved it itself
    # (final_submit never trusts the model's id for the irreversible click).
    resolved_control_id: str = ""
    # A planner mistake rather than a governance wall: the loop may re-observe
    # and re-plan (bounded) instead of ending the run.
    replan: bool = False


def origin_of(url: str) -> str:
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def page_class(obs: Observation) -> str:
    """Deterministic class for this observation. Order is fail-closed."""
    blob = _blob(obs)
    if _CAPTCHA.search(blob) or any(_CAPTCHA.search(c.identity) for c in obs.controls):
        return PAGE_CAPTCHA
    if any(c.field_type == "password" for c in obs.controls) or _LOGIN.search(blob):
        return PAGE_LOGIN
    if _PAYMENT.search(blob):
        return PAGE_PAYMENT
    if _self_id_required(obs, blob):
        return PAGE_SELF_ID
    if _RECEIPT.search(obs.body_text or "") or _RECEIPT.search(obs.title or ""):
        return PAGE_RECEIPT
    if _looks_like_application(obs):
        return PAGE_APPLICATION
    return PAGE_UNKNOWN


def allowed_actions(page: str, *, allow_submit: bool) -> frozenset[str]:
    if page == PAGE_RECEIPT:
        return frozenset({"stop"})
    if page in HANDOFF_PAGES:
        return frozenset({"stop"})
    if page == PAGE_APPLICATION:
        allowed = {"fill", "select", "upload", "check", "click_next", "dismiss", "stop"}
        if allow_submit:
            allowed.add("final_submit")
        return frozenset(allowed)
    return frozenset({"stop"})


def check(
    action: Action,
    *,
    obs: Observation,
    ctx: SubmissionContext,
    allow_submit: bool,
    start_origin: str,
    resume_path: str = "",
    cover_path: str = "",
) -> Decision:
    klass = page_class(obs)
    if klass in HANDOFF_PAGES and action.type != "stop":
        return Decision(False, reason=f"page class {klass} is a hand-off", page_class=klass)
    if action.type not in allowed_actions(klass, allow_submit=allow_submit):
        return Decision(
            False,
            reason=f"{action.type} is not legal on {klass}",
            page_class=klass,
        )
    if action.type == "stop":
        return Decision(True, page_class=klass)
    if origin_of(obs.url) and start_origin and origin_of(obs.url) != start_origin:
        return Decision(False, reason="off-origin navigation", page_class=klass)
    if action.type == "final_submit":
        if not allow_submit:
            return Decision(False, reason="final_submit is disabled on this run", page_class=klass)
        if klass != PAGE_APPLICATION:
            return Decision(False, reason="final_submit is not legal on this page", page_class=klass)
        blanks = required_blanks(ctx)
        if blanks:
            return Decision(
                False,
                reason="required questions unanswered: " + "; ".join(blanks[:5]),
                page_class=klass,
            )
        if any(_SIGNATURE.search(c.identity) for c in obs.controls):
            return Decision(False, reason="legal signature is a hand-off", page_class=klass)
        # The page's own required controls, checked deterministically now that
        # observations carry current state: a run submitted with the required
        # choice questions untouched (Forerunner, 2026-08-27). Re-plannable —
        # the planner gets the list and fills them.
        empty_required = [
            c.id
            for c in obs.controls
            if c.required
            and c.field_type not in {"button", "submit", "a", "link", "reset", "image"}
            and not c.value
            and not c.checked
        ]
        if empty_required:
            return Decision(
                False,
                reason="required page controls are empty: " + ", ".join(empty_required[:6]),
                page_class=klass,
                replan=True,
            )
        # The model's control_id is never trusted for the one irreversible
        # click: it invented a ">FINAL" placeholder live (Forerunner,
        # 2026-08-27) while the real "Submit Application" button sat in the
        # inventory. The model decides WHETHER to submit; which button is the
        # send control is checkable page truth, resolved here.
        control = _submit_control(obs)
        if control is None:
            return Decision(False, reason="no submit control on this page", page_class=klass)
        if _CREATE_ACCOUNT.search(control.label) or _CREATE_ACCOUNT.search(control.identity):
            return Decision(
                False, reason="submit control looks like account creation", page_class=klass
            )
        return Decision(True, page_class=klass, resolved_control_id=control.id)

    control = _control(obs, action.control_id)
    if control is None:
        return Decision(False, reason="unknown control_id", page_class=klass, replan=True)
    mismatch = _action_type_mismatch(action.type, control)
    if mismatch:
        # The model answered a Yes/No *button* with fill and Playwright threw
        # mid-run (live, Forerunner 2026-08-27). Wrong-verb-for-control is a
        # planner mistake; name the right verb and let it re-plan.
        return Decision(False, reason=mismatch, page_class=klass, replan=True)
    if action.type in FILL_TYPES:
        resolved, file_path, err = resolve_token(
            action.value_token,
            ctx,
            resume_path=resume_path,
            cover_path=cover_path,
            control=control,
        )
        if err:
            return Decision(False, reason=err, page_class=klass)
        if _forbidden_control(control, resolved_value=resolved):
            return Decision(False, reason=f"control {control.id} is a forbidden field", page_class=klass)
        if action.type == "upload" and not file_path:
            return Decision(False, reason="upload requires a document token", page_class=klass)
        if action.type != "check" and not resolved and not file_path:
            return Decision(False, reason="empty token", page_class=klass)
        return Decision(
            True, resolved_value=resolved, resolved_file=file_path, page_class=klass
        )
    if action.type in CLICK_TYPES:
        if action.type == "click_next" and _looks_like_submit(control) and allow_submit is False:
            return Decision(
                False, reason="preflight cannot click a submit control", page_class=klass
            )
        if action.type == "click_next" and _looks_like_submit(control):
            return Decision(False, reason="use final_submit for the send control", page_class=klass)
        if _forbidden_control(control, resolved_value=control.label):
            return Decision(
                False, reason=f"control {control.id} is a forbidden field", page_class=klass
            )
        return Decision(True, page_class=klass)
    return Decision(False, reason="unrecognised action", page_class=klass)


def resolve_token(
    token: str,
    ctx: SubmissionContext,
    *,
    resume_path: str = "",
    cover_path: str = "",
    control: Control | None = None,
) -> tuple[str, str, str]:
    """``(value, file_path, error)``. Empty token or unknown token errors."""
    token = (token or "").strip()
    if not token:
        return "", "", "missing value_token"
    if token == POLICY_DECLINE:
        choice = decline_choice(control.options if control is not None else ())
        if not choice:
            return "", "", "no decline option on this control"
        return choice, "", ""
    if token in CANDIDATE_TOKENS:
        return _candidate_value(ctx, CANDIDATE_TOKENS[token]), "", ""
    if token in DOC_TOKENS:
        which = DOC_TOKENS[token]
        path = resume_path if which == "resume" else cover_path
        if not path:
            return "", "", f"no {which} artifact on this run"
        return "", path, ""
    if is_answer_token(token):
        key = answer_key(token)
        value = ctx.answer(key)
        if not str(value or "").strip():
            return "", "", f"no approved answer for {key}"
        return str(value), "", ""
    if token.startswith(ANSWER_PREFIX) or token.startswith("candidate.") or token.startswith("doc.") or token.startswith("policy."):
        return "", "", f"unknown token {token}"
    return "", "", "invented value is not allowed"


def required_blanks(ctx: SubmissionContext) -> list[str]:
    labels: list[str] = []
    for entry in ctx.answers:
        if entry.get("kind") not in ("custom", "attestation"):
            continue
        if str(entry.get("answer") or "").strip():
            continue
        if entry.get("form_required"):
            labels.append(str(entry.get("question") or entry.get("key")))
    return labels


def looks_like_submit(control: Control) -> bool:
    text = f"{control.label} {control.identity} {control.role}"
    if _CREATE_ACCOUNT.search(text):
        return False
    if _NEXT.search(control.label):
        return False
    return bool(_SUBMIT.search(text) or control.role == "submit")


def _candidate_value(ctx: SubmissionContext, key: str) -> str:
    mapping = {
        "full_name": ctx.full_name,
        "first_name": ctx.first_name,
        "last_name": ctx.last_name,
        "email": ctx.from_address,
        "phone": ctx.phone,
        "linkedin": ctx.linkedin_url,
        "location": ctx.location,
    }
    return str(mapping.get(key) or "")


def _control(obs: Observation, control_id: str) -> Control | None:
    if not control_id:
        return None
    for control in obs.controls:
        if control.id == control_id:
            return control
    return None


def _submit_control(obs: Observation) -> Control | None:
    for control in obs.controls:
        if control.role in {"submit", "button"} and looks_like_submit(control):
            return control
    return None


def _looks_like_submit(control: Control) -> bool:
    return looks_like_submit(control)


#: field_type -> the verb that actually works on it. fill is only for
#: text-shaped inputs; everything else has its own action.
_VERB_FOR_TYPE = {
    "checkbox": "check",
    "radio": "check",
    "file": "upload",
    "select": "select",
    "select-one": "select",
    "button": "click_next",
    "submit": "final_submit",
    "reset": "click_next",
    "image": "click_next",
    "a": "click_next",
    "link": "click_next",
}


def _action_type_mismatch(action_type: str, control: Control) -> str:
    """Why this verb cannot act on this control, or empty when it can.

    Every branch names the verb that DOES work: "not a select" alone had the
    planner re-proposing select on the same checkbox until its fault budget
    died (live, Forerunner 2026-08-27) — the refusal is the steering, so it
    has to steer.
    """
    expected = _VERB_FOR_TYPE.get(control.field_type) or "fill"
    if action_type == "fill" and control.field_type in _VERB_FOR_TYPE:
        return f"control {control.id} is a {control.field_type} — use {expected}"
    if action_type == "upload" and control.field_type != "file":
        return f"control {control.id} is a {control.field_type}, not a file input — use {expected}"
    if action_type == "select" and control.field_type not in {"select", "select-one"}:
        return f"control {control.id} is a {control.field_type}, not a select — use {expected}"
    if action_type == "check" and control.field_type not in {"checkbox", "radio"}:
        return f"control {control.id} is a {control.field_type}, not checkable — use {expected}"
    return ""


def _forbidden_control(control: Control, *, resolved_value: str = "") -> bool:
    blob = control.identity
    if control.field_type == "password":
        return True
    if _PASSWORD.search(blob) or _CAPTCHA.search(blob) or _PAYMENT.search(blob):
        return True
    if _SELF_ID.search(blob):
        # A decline option is not a protected attribute. Anything else is.
        return not is_decline(resolved_value)
    return bool(_SIGNATURE.search(blob) and control.field_type in {"text", "checkbox"})


def _self_id_required(obs: Observation, blob: str) -> bool:
    hits = [c for c in obs.controls if _SELF_ID.search(c.identity)]
    if not hits:
        return bool(_SELF_ID.search(blob) and any(c.required for c in obs.controls))
    return any(c.required and not decline_choice(c.options) for c in hits)


def _looks_like_application(obs: Observation) -> bool:
    types = {c.field_type for c in obs.controls}
    labels = " ".join(c.label for c in obs.controls)
    if "file" in types and re.search(r"resum|curriculum|\bcv\b", labels, re.I):
        return True
    if any(c.field_type == "email" or "email" in c.label.lower() for c in obs.controls):
        if any("name" in c.label.lower() for c in obs.controls):
            return True
        if "file" in types:
            return True
    return bool(
        obs.controls and any(_NEXT.search(c.label) or _SUBMIT.search(c.label) for c in obs.controls)
    )


def _blob(obs: Observation) -> str:
    parts = [obs.url, obs.title, obs.body_text[:2000]]
    for control in obs.controls:
        parts.append(control.identity)
    return "\n".join(parts)
