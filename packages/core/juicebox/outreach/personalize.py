"""Build the variable bag a sequence template renders against.

Deterministic fields (name, title, company) always come from the profile. The
three written fragments — opening line, relevance sentence, subject hint — come
from the model when one is configured and from a template otherwise.
"""

from __future__ import annotations

import logging
from typing import Any

from ..llm.client import get_llm
from ..llm.prompts import PERSONALIZE_SCHEMA, PERSONALIZE_SYSTEM, personalize_prompt
from ..models import Candidate, Project
from ..search.index import candidate_to_doc

logger = logging.getLogger(__name__)


def base_context(candidate: Candidate, project: Project | None = None) -> dict[str, Any]:
    doc = candidate_to_doc(candidate)
    top_skills = sorted(doc.skills)[:3]
    previous = next(
        (e for e in doc.companies if not e.get("is_current") and e.get("company")), None
    )
    return {
        "first_name": candidate.first_name or candidate.full_name.split(" ")[0],
        "last_name": candidate.last_name,
        "full_name": candidate.full_name,
        "current_title": candidate.current_title,
        "current_company": candidate.current_company,
        "previous_company": (previous or {}).get("company", ""),
        "location": ", ".join(doc.location_parts),
        "city": candidate.location_city,
        "years_experience": f"{candidate.years_experience:g}",
        "seniority": candidate.seniority,
        "top_skill": top_skills[0] if top_skills else "",
        "top_skills": ", ".join(top_skills),
        "role_title": project.role_title if project else "",
        "project_name": project.name if project else "",
        "company_location": project.location if project else "",
    }


def heuristic_fragments(candidate: Candidate, project: Project | None) -> dict[str, Any]:
    """Specific-but-safe copy built purely from profile fields."""
    doc = candidate_to_doc(candidate)
    skill = sorted(doc.skills)[0] if doc.skills else ""
    company = candidate.current_company
    title = candidate.current_title or "your work"
    role = (project.role_title if project else "") or "a role on our team"

    if company and skill:
        opening = f"I came across your {title} work at {company} — the {skill} side of it stood out."
    elif company:
        opening = f"I came across your {title} work at {company} and wanted to reach out."
    else:
        opening = f"I came across your background in {title} and wanted to reach out."

    if skill:
        why = f"We're hiring for {role} and your depth in {skill} lines up closely."
    else:
        why = f"We're hiring for {role} and your background looks like a strong match."

    subject = f"{role} — quick question" if project else "Quick question about your next move"

    return {
        "opening_line": opening,
        "why_relevant": why,
        "subject_hint": subject,
        "hooks": [h for h in (company, skill, title) if h],
        "personalized_by": "heuristic",
    }


def _role_context(project: Project | None) -> str:
    if project is None:
        return "An unspecified engineering role at a technology company."
    parts = [f"Role: {project.role_title or project.name}"]
    if project.location:
        parts.append(f"Location: {project.location}")
    if project.description:
        parts.append(f"Description: {project.description}")
    return "\n".join(parts)


def personalize(
    candidate: Candidate, project: Project | None = None, *, use_llm: bool | None = None
) -> dict[str, Any]:
    """Full variable bag: deterministic fields plus written fragments."""
    context = base_context(candidate, project)
    llm = get_llm()
    want_llm = llm.available if use_llm is None else (use_llm and llm.available)

    fragments: dict[str, Any] | None = None
    if want_llm:
        try:
            doc = candidate_to_doc(candidate)
            result = llm.json(
                system=PERSONALIZE_SYSTEM,
                prompt=personalize_prompt(
                    profile=doc.profile_text(), role_context=_role_context(project)
                ),
                schema=PERSONALIZE_SCHEMA,
                effort="low",
                max_tokens=4000,
            )
            data = result.data or {}
            if data.get("opening_line"):
                fragments = {
                    "opening_line": str(data.get("opening_line", "")).strip(),
                    "why_relevant": str(data.get("why_relevant", "")).strip(),
                    "subject_hint": str(data.get("subject_hint", "")).strip(),
                    "hooks": [str(h) for h in (data.get("hooks") or [])],
                    "personalized_by": "llm",
                }
        except Exception as exc:  # noqa: BLE001 - fall back rather than block a send
            logger.warning("personalisation failed for %s: %s", candidate.id, exc)

    context.update(fragments or heuristic_fragments(candidate, project))
    return context
