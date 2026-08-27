"""Ask the Juicebox LLM for one typed next action.

The screenshot is sent as an image part. The model may only return the
action schema; values are tokens, not answers. ``json_fast`` is forbidden
here — a submit click is not a latency budget.
"""

from __future__ import annotations

import logging

from .....config import get_settings
from .....llm import client as llm_mod
from .actions import ACTION_SCHEMA, Action, parse_action
from .observe import Observation
from .policy import allowed_actions, page_class

logger = logging.getLogger(__name__)

_SYSTEM = """You are filling a public job-application form on behalf of a candidate.
You propose exactly one next action. You never invent text, selectors, URLs,
scripts, passwords, or answers. Fill and upload only by naming a control id
from the inventory and a value_token from the allowed list.

Allowed value_token values: candidate.full_name, candidate.first_name,
candidate.last_name, candidate.email, candidate.phone, candidate.linkedin,
candidate.location, doc.resume, doc.cover_letter, policy.decline (only on
voluntary self-identification controls that offer Prefer not to answer),
and answer.<key> for keys listed as approved answers.

final_submit is the send control and may only be used when the inventory
shows a complete application page. click_next is for wizard continuation,
never for Submit / Send application / Apply now. stop if the page is a
login, CAPTCHA, payment, legal signature, or required self-identification
with no decline option, or if you cannot proceed honestly.

Respond with JSON matching the schema. No prose."""


def plan(
    obs: Observation,
    *,
    allow_submit: bool,
    approved_answer_keys: tuple[str, ...] = (),
    planner=None,
    feedback: str = "",
) -> Action | None:
    klass = page_class(obs)
    legal = sorted(allowed_actions(klass, allow_submit=allow_submit))
    steer = f"your_last_proposal_was_refused: {feedback}\n" if feedback.strip() else ""
    prompt = (
        f"page_class={klass}\n"
        f"legal_actions={legal}\n"
        f"approved_answer_keys={list(approved_answer_keys)}\n"
        f"{steer}"
        f"observation={obs.for_planner()}\n"
        "Propose the single next action."
    )
    llm = planner if planner is not None else _llm()
    if llm is None:
        return None
    try:
        result = llm.json(
            system=_SYSTEM,
            prompt=prompt,
            schema=ACTION_SCHEMA,
            effort="medium",
            max_tokens=800,
            model=_model(),
            image=obs.screenshot or None,
        )
    except Exception as exc:  # noqa: BLE001 - planner miss is a stop, not a crash
        logger.warning("planner call failed: %s", str(exc)[:300])
        return None
    action = parse_action(result.data if result is not None else None)
    if action is None:
        logger.warning(
            "planner returned unparseable action: %r", str(result.data)[:300] if result else None
        )
    return action


def available() -> bool:
    llm = _llm()
    return llm is not None and bool(getattr(llm, "available", False))


def _llm():
    try:
        return llm_mod.get_llm()
    except Exception:  # noqa: BLE001
        return None


def _model() -> str | None:
    configured = (get_settings().apply_browser_agent_model or "").strip()
    return configured or None
