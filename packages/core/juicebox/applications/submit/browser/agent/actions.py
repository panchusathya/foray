"""Typed next actions the planner may propose.

The model never supplies a selector, a script, a URL, a credential, or a
free-form answer. It names a control id we issued on this observation and,
when filling, an opaque token we resolve from ``SubmissionContext``.
"""

from __future__ import annotations

from dataclasses import dataclass

ACTION_TYPES = (
    "fill",
    "select",
    "upload",
    "check",
    "click_next",
    "dismiss",
    "final_submit",
    "stop",
)

FILL_TYPES = frozenset({"fill", "select", "upload", "check"})
CLICK_TYPES = frozenset({"click_next", "dismiss", "final_submit"})

CANDIDATE_TOKENS = {
    "candidate.full_name": "full_name",
    "candidate.first_name": "first_name",
    "candidate.last_name": "last_name",
    "candidate.email": "email",
    "candidate.phone": "phone",
    "candidate.linkedin": "linkedin",
    "candidate.location": "location",
}

DOC_TOKENS = {
    "doc.resume": "resume",
    "doc.cover_letter": "cover_letter",
}

ANSWER_PREFIX = "answer."

ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "type": {"type": "string", "enum": list(ACTION_TYPES)},
        "control_id": {"type": "string"},
        "value_token": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["type"],
}


@dataclass(frozen=True)
class Action:
    type: str
    control_id: str = ""
    value_token: str = ""
    reason: str = ""


def parse_action(data: dict | None) -> Action | None:
    """A typed action, or ``None`` if the model produced nothing usable."""
    if not isinstance(data, dict):
        return None
    kind = str(data.get("type") or "").strip()
    if kind not in ACTION_TYPES:
        return None
    return Action(
        type=kind,
        control_id=str(data.get("control_id") or "").strip(),
        value_token=str(data.get("value_token") or "").strip(),
        reason=str(data.get("reason") or "").strip()[:200],
    )


def is_answer_token(token: str) -> bool:
    return token.startswith(ANSWER_PREFIX) and len(token) > len(ANSWER_PREFIX)


def answer_key(token: str) -> str:
    return token[len(ANSWER_PREFIX) :] if is_answer_token(token) else ""
