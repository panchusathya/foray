"""One short cover letter, first person as the candidate.

Template fallback included and complete: three plain paragraphs built from the
resume JSON and the posting, with nothing to fill in by hand. Flat is fine;
fabricated is not.
"""

from __future__ import annotations

import json
import logging

from ..llm.client import get_llm
from ..llm.prompts import COVER_LETTER_SCHEMA, COVER_LETTER_SYSTEM, cover_letter_prompt
from ..models import JobPosting

logger = logging.getLogger(__name__)


def build_cover_letter(
    resume_json: dict, posting: JobPosting, *, role_spec_text: str = ""
) -> tuple[str, str]:
    """``(body, generated_by)`` where generated_by is llm|template."""
    llm = get_llm()
    if llm.available:
        try:
            result = llm.json(
                system=COVER_LETTER_SYSTEM,
                prompt=cover_letter_prompt(
                    resume_json=json.dumps(resume_json, ensure_ascii=False),
                    role_spec=role_spec_text or f"{posting.title} at {posting.company}",
                    company=posting.company or "",
                    title=posting.title or "",
                ),
                schema=COVER_LETTER_SCHEMA,
            )
            body = str((result.data or {}).get("body") or "").strip()
            if body and "{" not in body:
                return body, "llm"
            logger.warning("cover letter rejected (empty or placeholder leak), using template")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cover letter generation failed, using template: %s", exc)
    return _template_letter(resume_json, posting), "template"


def _template_letter(resume_json: dict, posting: JobPosting) -> str:
    contact = resume_json.get("contact") or {}
    name = str(contact.get("name") or "").strip()
    experience = resume_json.get("experience") or []
    current = experience[0] if experience else {}
    current_line = ""
    if isinstance(current, dict) and current.get("title") and current.get("company"):
        current_line = (
            f" I am currently {current['title']} at {current['company']}, and my background"
            " maps directly onto what this role calls for."
        )

    skills = [str(s) for s in (resume_json.get("skills") or [])[:3] if s]
    skills_line = (
        f" My core strengths are {', '.join(skills)}." if skills else ""
    )

    role = f"the {posting.title} role" if posting.title else "this role"
    company = f" at {posting.company}" if posting.company else ""

    return (
        "Dear Hiring Manager,\n\n"
        f"I am writing to apply for {role}{company}.{current_line}\n\n"
        f"My experience is set out in the attached resume.{skills_line} "
        "I would welcome the chance to walk through how it applies to your team's work.\n\n"
        "Thank you for your consideration.\n\n"
        f"Sincerely,\n{name}"
    )
