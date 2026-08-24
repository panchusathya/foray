"""The standard application-form answer set, with provenance per answer.

CANDIDATE_AGENT_PLAN §8: every generated answer names the resume line, intake
field, or message that supports it, and **no answer is generated from
nothing**. The deterministic path copies preference values verbatim;
why-this-company is left blank for the human rather than invented — a blank
reads as honest, a fabricated enthusiasm reads as somebody else's voice.
"""

from __future__ import annotations

import json
import logging

from ..llm.client import get_llm
from ..llm.prompts import FORM_ANSWERS_SCHEMA, FORM_ANSWERS_SYSTEM, form_answers_prompt
from ..models import CandidatePreference, JobPosting

logger = logging.getLogger(__name__)

STANDARD_QUESTIONS: tuple[tuple[str, str], ...] = (
    (
        "work_authorization",
        "Are you legally authorized to work in this country, and will you need "
        "sponsorship now or in the future?",
    ),
    ("salary_expectation", "What are your compensation expectations?"),
    ("notice_period", "When could you start?"),
    ("why_company", "Why do you want to work at {company}?"),
)


def build_form_answers(
    *,
    preference: CandidatePreference | None,
    resume_json: dict,
    posting: JobPosting,
) -> tuple[list[dict], str]:
    """``(answers, generated_by)``; answers are
    ``{key, question, answer, provenance, generated_by, edited}`` dicts."""
    company = posting.company or "the company"
    deterministic = _deterministic_answers(preference, company=company)

    llm = get_llm()
    if not llm.available:
        return deterministic, "template"

    questions = "\n".join(
        f"- {key}: {question.format(company=company)}" for key, question in STANDARD_QUESTIONS
    )
    try:
        result = llm.json(
            system=FORM_ANSWERS_SYSTEM,
            prompt=form_answers_prompt(
                questions=questions,
                preference_context=_preference_context(preference),
                resume_json=json.dumps(resume_json, ensure_ascii=False),
            ),
            schema=FORM_ANSWERS_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - deterministic answers ship instead
        logger.warning("form answers failed, using deterministic set: %s", exc)
        return deterministic, "template"

    by_key = {str(item.get("key")): item for item in (result.data or {}).get("answers") or []}
    merged: list[dict] = []
    for key, question in STANDARD_QUESTIONS:
        model_answer = by_key.get(key) or {}
        answer = str(model_answer.get("answer") or "").strip()
        provenance = str(model_answer.get("provenance") or "none").strip()
        if not answer or provenance == "none":
            # The prompt's contract: no provenance, no assertion. Fall back to
            # the deterministic value for this key (which may itself be blank).
            fallback = next(d for d in deterministic if d["key"] == key)
            merged.append(fallback)
            continue
        merged.append(
            {
                "key": key,
                "question": question.format(company=company),
                "answer": answer,
                "provenance": provenance,
                "generated_by": "llm",
                "edited": False,
            }
        )
    return merged, "llm"


def _deterministic_answers(
    preference: CandidatePreference | None, *, company: str
) -> list[dict]:
    def entry(key: str, question: str, answer: str, provenance: str) -> dict:
        return {
            "key": key,
            "question": question.format(company=company),
            "answer": answer,
            "provenance": provenance if answer else "none",
            "generated_by": "template",
            "edited": False,
        }

    work_auth = ""
    salary = ""
    notice = ""
    if preference is not None:
        work_auth = (preference.work_authorization or "").strip()
        if preference.target_base or preference.target_total:
            amount = preference.target_total or preference.target_base
            currency = (preference.comp_currency or "").strip()
            salary = f"{currency} {amount:,.0f}".strip()
        notice = (preference.timeline or "").strip()

    questions = dict(STANDARD_QUESTIONS)
    return [
        entry(
            "work_authorization",
            questions["work_authorization"],
            work_auth,
            "preference:work_authorization",
        ),
        entry(
            "salary_expectation",
            questions["salary_expectation"],
            salary,
            "preference:target_total",
        ),
        entry("notice_period", questions["notice_period"], notice, "preference:timeline"),
        # Deliberately blank: enthusiasm is the one thing we never write for
        # somebody without a model reading their actual background — and even
        # then only with provenance.
        entry("why_company", questions["why_company"], "", "none"),
    ]


def _preference_context(preference: CandidatePreference | None) -> str:
    if preference is None:
        return ""
    lines = []
    if preference.work_authorization:
        lines.append(f"work_authorization: {preference.work_authorization}")
    if preference.timeline:
        lines.append(f"timeline: {preference.timeline}")
    if preference.target_base:
        lines.append(f"target_base: {preference.target_base}")
    if preference.target_total:
        lines.append(f"target_total: {preference.target_total}")
    if preference.comp_currency:
        lines.append(f"comp_currency: {preference.comp_currency}")
    if preference.willing_to_relocate is not None:
        lines.append(f"willing_to_relocate: {preference.willing_to_relocate}")
    if preference.highlights:
        lines.append("highlights: " + "; ".join(str(h) for h in preference.highlights if h))
    return "\n".join(lines)
