"""Draft a sequence from a short brief.

The model writes the draft when one is configured; otherwise a seeded template
library fills in, so "Generate with AI" still produces something usable offline.
Nothing here persists — the caller gets a ``SequenceIn``-shaped dict to edit
and POST through the normal create endpoint.
"""

from __future__ import annotations

import logging
from typing import Any

from ..llm.client import get_llm
from ..llm.prompts import (
    DRAFT_SEQUENCE_SCHEMA,
    DRAFT_SEQUENCE_SYSTEM,
    draft_sequence_prompt,
)
from ..models import Project

logger = logging.getLogger(__name__)

MAX_STEPS = 4

# The offline fallback: a proven plain-prose outreach arc (intro → nudge → close-out).
# Steps are trimmed to the requested count from the front, keeping the close-out last.
_TEMPLATE_STEPS: list[dict[str, Any]] = [
    {
        "subject_template": "{{subject_hint|Quick question about your next move}}",
        "body_template": (
            "Hi {{first_name|there}},\n\n"
            "{{opening_line}}\n\n"
            "We're hiring for {{role_title|a role on our team}}. {{why_relevant}} "
            "If you're open to it, I'd like to share more.\n\n"
            "Worth a short call this week?"
        ),
        "delay_days": 0,
        "delay_unit": "business_days",
    },
    {
        "subject_template": "Re: {{subject_hint|your next move}}",
        "body_template": (
            "Hi {{first_name|there}},\n\n"
            "Following up in case my last note got buried. The {{role_title|role}} is "
            "still open and your {{top_skill|background}} experience is why I reached "
            "out specifically.\n\n"
            "Happy to send details over email if that's easier than a call."
        ),
        "delay_days": 3,
        "delay_unit": "business_days",
    },
    {
        "subject_template": "Closing the loop",
        "body_template": (
            "Hi {{first_name|there}},\n\n"
            "Last note from me. If the timing is wrong or this isn't interesting, no "
            "response needed — I won't follow up again.\n\n"
            "If it is interesting later, my inbox is open."
        ),
        "delay_days": 4,
        "delay_unit": "business_days",
    },
]


def _role_context(project: Project | None, brief: str) -> str:
    if project is None:
        return brief
    parts = [f"Role: {project.role_title or project.name}"]
    if project.location:
        parts.append(f"Location: {project.location}")
    if project.description:
        parts.append(f"Description: {project.description}")
    return "\n".join(parts)


def _template_draft(brief: str, project: Project | None, num_steps: int) -> dict[str, Any]:
    role = (project.role_title or project.name) if project else ""
    name = f"{role} outreach" if role else "Outreach sequence"
    steps = list(_TEMPLATE_STEPS)
    if num_steps < len(steps):
        # Keep the close-out as the final step when trimming.
        steps = [*steps[: num_steps - 1], steps[-1]] if num_steps > 1 else steps[:1]
    return {"name": name[:200], "steps": steps}


def _coerce_steps(raw: Any, num_steps: int) -> list[dict[str, Any]]:
    """Clamp model output into valid ``SequenceStepIn`` shapes; [] means unusable."""
    if not isinstance(raw, list):
        return []
    steps: list[dict[str, Any]] = []
    for item in raw[:num_steps]:
        if not isinstance(item, dict):
            continue
        body = str(item.get("body_template", "")).strip()
        if not body:
            continue
        try:
            delay = int(item.get("delay_days", 0))
        except (TypeError, ValueError):
            delay = 0
        unit = item.get("delay_unit")
        steps.append(
            {
                "subject_template": str(item.get("subject_template", "")).strip(),
                "body_template": body,
                "delay_days": min(max(delay, 0), 90),
                "delay_unit": unit if unit in ("days", "business_days") else "days",
            }
        )
    if steps:
        steps[0]["delay_days"] = 0
    return steps


def draft_sequence(
    *,
    brief: str,
    project: Project | None = None,
    num_steps: int = 3,
) -> tuple[dict[str, Any], str]:
    """Return (SequenceIn-shaped dict, generated_by)."""
    num_steps = min(max(num_steps, 1), MAX_STEPS)
    llm = get_llm()
    if llm.available:
        try:
            result = llm.json(
                system=DRAFT_SEQUENCE_SYSTEM,
                prompt=draft_sequence_prompt(
                    brief=brief, role_context=_role_context(project, brief), num_steps=num_steps
                ),
                schema=DRAFT_SEQUENCE_SCHEMA,
                effort="medium",
                max_tokens=4000,
            )
            data = result.data or {}
            steps = _coerce_steps(data.get("steps"), num_steps)
            if steps:
                name = str(data.get("name", "")).strip() or "Outreach sequence"
                return {"name": name[:200], "steps": steps}, "llm"
        except Exception as exc:  # noqa: BLE001 - fall back rather than block drafting
            logger.warning("sequence drafting failed, using template: %s", exc)
    return _template_draft(brief, project, num_steps), "template"
