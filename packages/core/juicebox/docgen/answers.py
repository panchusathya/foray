"""The application-form answer set, with provenance per answer.

CANDIDATE_AGENT_PLAN §8: every generated answer names the resume line, intake
field, or message that supports it, and **no answer is generated from
nothing**. The deterministic path copies preference values verbatim;
why-this-company is left blank for the human rather than invented — a blank
reads as honest, a fabricated enthusiasm reads as somebody else's voice.

When the posting carries its form's own questions (apply-by-link ingest,
``JobPosting.form_questions``), those are answered too — under the same
provenance contract. Two carve-outs are structural, not stylistic:
self-identification questions (gender, ethnicity, veteran status, ...) are
carried so the candidate sees the whole form but are **never answered** —
INTAKE_COMPLIANCE forbids collecting or asserting protected attributes — and
file questions are skipped because the resume and cover letter are the
package's own documents.
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


#: A form question that is one of the standard four in the employer's words.
#: Matching one replaces the standard wording under the same key, so
#: ``followups.ASKABLE`` and ``refill_from_preference`` keep working on forms
#: we did not write.
_ALIAS_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("salary_expectation", ("salary", "compensation", "pay expectation", "pay range")),
    (
        "work_authorization",
        ("sponsor", "authoriz", "authorised to work", "visa", "right to work"),
    ),
    ("notice_period", ("notice period", "start date", "when can you start", "available to start")),
    ("why_company", ("why do you want", "why are you interested", "why us")),
)


def _standard_alias(label: str) -> str | None:
    lowered = label.lower()
    for key, needles in _ALIAS_PATTERNS:
        if any(needle in lowered for needle in needles):
            return key
    return None


def question_set(posting: JobPosting) -> list[dict]:
    """The full ordered question list: the standard four, then the form's own.

    ``{key, question, self_identify, options}`` dicts. File questions never
    appear (the package generates those as documents); self-identification
    questions appear flagged, so the answer set can show them unanswered.
    """
    company = posting.company or "the company"
    questions: list[dict] = [
        {"key": key, "question": text.format(company=company), "self_identify": False, "options": []}
        for key, text in STANDARD_QUESTIONS
    ]
    by_key = {entry["key"]: entry for entry in questions}
    for raw in posting.form_questions or []:
        label = str(raw.get("question") or "").strip()
        if not label or str(raw.get("kind") or "") == "file":
            continue
        options = [str(option) for option in (raw.get("options") or [])][:12]
        alias = None if raw.get("self_identify") else _standard_alias(label)
        if alias is not None:
            by_key[alias]["question"] = label
            by_key[alias]["options"] = options
            continue
        questions.append(
            {
                "key": str(raw.get("key") or f"form_q{len(questions)}"),
                "question": label,
                "self_identify": bool(raw.get("self_identify")),
                "options": options,
            }
        )
    return questions


def build_form_answers(
    *,
    preference: CandidatePreference | None,
    resume_json: dict,
    posting: JobPosting,
) -> tuple[list[dict], str]:
    """``(answers, generated_by)``; answers are
    ``{key, question, answer, provenance, generated_by, edited}`` dicts."""
    company = posting.company or "the company"
    questions = question_set(posting)
    deterministic = {d["key"]: d for d in _deterministic_answers(preference, company=company)}

    def _blank(entry: dict, *, generated_by: str) -> dict:
        blank = {
            "key": entry["key"],
            "question": entry["question"],
            "answer": "",
            "provenance": "none",
            "generated_by": generated_by,
            "edited": False,
        }
        if entry["self_identify"]:
            blank["self_identify"] = True
        return blank

    base: list[dict] = []
    for entry in questions:
        if entry["self_identify"]:
            # Never answered, whatever the model path says: a protected
            # attribute asserted in somebody's name is a compliance failure,
            # not a blank to fill.
            base.append(_blank(entry, generated_by="policy"))
            continue
        known = deterministic.get(entry["key"])
        if known is not None:
            known = dict(known)
            known["question"] = entry["question"]
            base.append(known)
        else:
            base.append(_blank(entry, generated_by="template"))

    llm = get_llm()
    if not llm.available:
        return base, "template"

    lines = []
    for entry in questions:
        if entry["self_identify"]:
            continue
        line = f"- {entry['key']}: {entry['question']}"
        if entry["options"]:
            line += " (answer with one of: " + "; ".join(entry["options"]) + ")"
        lines.append(line)
    try:
        result = llm.json(
            system=FORM_ANSWERS_SYSTEM,
            prompt=form_answers_prompt(
                questions="\n".join(lines),
                preference_context=_preference_context(preference),
                resume_json=json.dumps(resume_json, ensure_ascii=False),
            ),
            schema=FORM_ANSWERS_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - deterministic answers ship instead
        logger.warning("form answers failed, using deterministic set: %s", exc)
        return base, "template"

    by_key = {str(item.get("key")): item for item in (result.data or {}).get("answers") or []}
    merged: list[dict] = []
    for entry, fallback in zip(questions, base, strict=True):
        if entry["self_identify"]:
            merged.append(fallback)
            continue
        model_answer = by_key.get(entry["key"]) or {}
        answer = str(model_answer.get("answer") or "").strip()
        provenance = str(model_answer.get("provenance") or "none").strip()
        if not answer or provenance == "none":
            # The prompt's contract: no provenance, no assertion. Fall back to
            # the deterministic value for this key (which may itself be blank).
            merged.append(fallback)
            continue
        merged.append(
            {
                "key": entry["key"],
                "question": entry["question"],
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
