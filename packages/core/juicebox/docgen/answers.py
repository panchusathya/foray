"""The application-form answer set, with provenance per answer.

CANDIDATE_AGENT_PLAN §8: every generated answer names the resume line, intake
field, or message that supports it, and **no answer is generated from
nothing**. What counts as evidence differs by question, and the difference is
the whole design:

**Facts about the candidate** (work authorization, comp, start date) may only
ever restate something the candidate said -- a ``preference:*`` source. The
first live package proved why citing a resume is not enough: the model
inferred "authorized to work, no sponsorship needed" from a teaching-assistant
job and a congressional internship, cited both, and the old
``provenance != "none"`` guard waved a fabricated legal attestation through.
Work authorization is the extreme case and is never generated at all: the
preference value verbatim, or blank and asked.

**The motivation question** (why this company) asserts no checkable fact, so
it is the one answer that is *drafted* -- from the resume and the posting,
inference allowed -- and flagged ``needs_review`` so the candidate reads it as
their own draft to edit, not our words in their mouth. It stays editable in
the portal until a submit attempt actually fires.

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

#: Keys whose answer is a fact about the candidate. The model may *phrase*
#: these, but only from a stated preference -- its provenance must name a
#: ``preference:*`` source or the answer is discarded for the deterministic
#: value.
FACT_KEYS = frozenset({"work_authorization", "salary_expectation", "notice_period"})

#: The subset of FACT_KEYS the model never touches at all. A visa/sponsorship
#: answer is a legal attestation in the candidate's name; there is no phrasing
#: of it worth the risk of the phrasing drifting from the stated fact.
NEVER_GENERATED = frozenset({"work_authorization"})

#: Keys whose answer is drafted -- inference from the resume and posting is
#: allowed because nothing in it asserts a checkable fact. Always flagged
#: ``needs_review``.
DRAFTED_KEYS = frozenset({"why_company"})

#: Where each standard entry came from, for surfaces that must distinguish
#: "the form asks this" from "we always ask this". Mirrors
#: applications.questions.ORIGIN_STANDARD/ORIGIN_FORM (kept local: importing
#: the applications package from docgen would be a cycle).
ORIGIN_STANDARD = "standard"
ORIGIN_FORM = "form"


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
        {
            "key": key,
            "question": text.format(company=company),
            "self_identify": False,
            "options": [],
            "origin": ORIGIN_STANDARD,
        }
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
            # The employer's wording of a standard question: the key (and
            # therefore origin) stays standard so followups keep working.
            by_key[alias]["question"] = label
            by_key[alias]["options"] = options
            continue
        questions.append(
            {
                "key": str(raw.get("key") or f"form_q{len(questions)}"),
                "question": label,
                "self_identify": bool(raw.get("self_identify")),
                "options": options,
                "origin": ORIGIN_FORM,
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
            "origin": entry["origin"],
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
        # Self-ID is never answered; NEVER_GENERATED is never even offered to
        # the model — the preference verbatim or blank.
        if entry["self_identify"] or entry["key"] in NEVER_GENERATED:
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
                posting_context=_posting_context(posting),
            ),
            schema=FORM_ANSWERS_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - deterministic answers ship instead
        logger.warning("form answers failed, using deterministic set: %s", exc)
        return base, "template"

    by_key = {str(item.get("key")): item for item in (result.data or {}).get("answers") or []}
    merged: list[dict] = []
    for entry, fallback in zip(questions, base, strict=True):
        key = entry["key"]
        if entry["self_identify"] or key in NEVER_GENERATED:
            # Self-ID is a compliance wall; NEVER_GENERATED was not even
            # offered to the model. The preference verbatim or blank.
            merged.append(fallback)
            continue
        model_answer = by_key.get(key) or {}
        answer = str(model_answer.get("answer") or "").strip()
        provenance = str(model_answer.get("provenance") or "none").strip()
        if not answer or provenance == "none":
            # The prompt's contract: no provenance, no assertion. Fall back to
            # the deterministic value for this key (which may itself be blank).
            merged.append(fallback)
            continue
        if key in FACT_KEYS and not provenance.startswith("preference:"):
            # A fact the candidate never stated. Citing the resume is how the
            # first live package fabricated a work-authorization claim: the
            # model *did* cite sources, they just did not support the answer.
            # Restating a preference is the only phrasing allowed here.
            logger.info(
                "discarding generated %s: provenance %r is not a stated preference",
                key,
                provenance,
            )
            merged.append(fallback)
            continue
        item = {
            "key": key,
            "question": entry["question"],
            "answer": answer,
            "provenance": provenance,
            "generated_by": "llm",
            "edited": False,
            "origin": entry["origin"],
        }
        if key in DRAFTED_KEYS:
            # A draft, not a fact: shown to the candidate as theirs to edit.
            item["needs_review"] = True
        merged.append(item)
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
            "origin": ORIGIN_STANDARD,
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
        # Blank only as the no-LLM fallback: the model drafts this one from
        # the resume and the posting (see DRAFTED_KEYS), and the candidate
        # reviews it in the portal. With no model there is nothing to draft
        # from, and a template cannot write somebody's motivation.
        entry("why_company", questions["why_company"], "", "none"),
    ]


def _posting_context(posting: JobPosting) -> str:
    """What the drafted answer may draw on: the role as the employer wrote it.

    Capped hard -- a JD is tens of KB of boilerplate and the draft needs the
    company, the role, and enough of the pitch to connect the resume to it.
    """
    lines = []
    if posting.company:
        lines.append(f"company: {posting.company}")
    if posting.title:
        lines.append(f"title: {posting.title}")
    if posting.location:
        lines.append(f"location: {posting.location}")
    jd = (posting.jd_text or "").strip()
    if jd:
        lines.append(f"description:\n{jd[:4000]}")
    return "\n".join(lines)


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
