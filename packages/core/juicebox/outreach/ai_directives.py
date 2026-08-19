"""Resolve ``{{ai: ...}}`` directives in a sequence step at send time.

One model call per step resolves every directive in it, with the candidate
profile, the role, the template's variable bag, and the earlier emails already
sent on this enrollment as context — that last part is what makes a follow-up
directive aware of what was already said.

Fail-closed by design: if the model is unavailable or returns an unusable
fragment, the caller gets ``LLMUnavailable`` and the send fails visibly. There
is no fallback sentence — the recruiter asked the model for that sentence, and
a canned substitute would go out under their name.

Prompt-injection posture: only *our own* sent bodies are included, never
inbound candidate text, and the profile is framed as data. Fragments are
post-validated so a bad one (URLs, template braces that ``render`` would
re-scan, absurd length) counts as a resolution failure rather than shipping.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..llm.client import LLMUnavailable, get_llm
from ..llm.prompts import AI_DIRECTIVE_SCHEMA, AI_DIRECTIVE_SYSTEM, ai_directive_prompt
from ..models import Candidate, Enrollment, Message, Project, Sequence, SequenceStep
from ..search.index import candidate_to_doc
from . import templates
from .personalize import _role_context

logger = logging.getLogger(__name__)

MAX_FRAGMENT_CHARS = 600
MAX_PRIOR_EMAILS = 4


def _clean(text: str) -> str:
    """Strip markup the model must not emit into an email body."""
    return text.replace("<", "").replace(">", "").strip()


def _fragment_ok(text: str) -> bool:
    if not text or len(text) > MAX_FRAGMENT_CHARS:
        return False
    lowered = text.lower()
    # Braces would be re-scanned by templates.render and could interpolate an
    # unintended variable; a URL in a model-written fragment is never wanted.
    return "{{" not in text and "}}" not in text and "http" not in lowered


def _prior_sent_bodies(db: Session, enrollment: Enrollment | None) -> str:
    if enrollment is None:
        return ""
    rows = db.scalars(
        select(Message)
        .where(
            Message.enrollment_id == enrollment.id,
            Message.direction == "outbound",
            Message.status == "sent",
        )
        .order_by(Message.created_at.asc())
        .limit(MAX_PRIOR_EMAILS)
    ).all()
    parts = []
    for index, message in enumerate(rows, 1):
        body = (message.body or "").strip()
        if body:
            parts.append(f'<previous_email step="{index}">\n{body}\n</previous_email>')
    return "\n".join(parts)


def resolve_directives(
    db: Session,
    *,
    sequence: Sequence,
    step: SequenceStep,
    candidate: Candidate,
    project: Project | None,
    enrollment: Enrollment | None,
    context: dict,
    prior: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolved text per directive key for this step. ``{}`` when the step has
    no directives; ``prior`` verbatim when it already covers every key (so a
    retried send reuses the exact text that may already have gone out)."""
    directives = templates.used_directives(
        f"{step.subject_template or ''}\n{step.body_template or ''}"
    )
    if not directives:
        return {}

    if prior and all(directive.key in prior for directive in directives):
        return {directive.key: prior[directive.key] for directive in directives}

    llm = get_llm()
    if not llm.available:
        raise LLMUnavailable(
            f"step has {{{{ai: ...}}}} directives but no model is available: "
            f"{llm.unavailable_reason}"
        )

    doc = candidate_to_doc(candidate)
    variables = "\n".join(
        f"{key}: {value}"
        for key, value in sorted(context.items())
        if isinstance(value, str | int | float) and value != "" and not key.startswith("_")
    )
    prompt = ai_directive_prompt(
        candidate_context=_clean(doc.profile_text()),
        role_context=_clean(_role_context(project)),
        variables=_clean(variables),
        step_number=step.step_order + 1,
        template_body=step.body_template or "",
        directives=[(d.key, d.instruction) for d in directives],
        prior_emails=_prior_sent_bodies(db, enrollment),
    )

    result = llm.json(
        system=AI_DIRECTIVE_SYSTEM,
        prompt=prompt,
        schema=AI_DIRECTIVE_SCHEMA,
        effort="medium",
        max_tokens=2000,
    )
    fragments = {
        str(item.get("key", "")): _clean(str(item.get("text", "")))
        for item in (result.data or {}).get("fragments", [])
    }

    resolved: dict[str, str] = {}
    for directive in directives:
        text = fragments.get(directive.key, "")
        if not _fragment_ok(text):
            raise LLMUnavailable(
                f"model produced no usable fragment for directive: "
                f"{directive.instruction[:80]}"
            )
        resolved[directive.key] = text
    return resolved
