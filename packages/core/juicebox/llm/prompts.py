"""System prompts and JSON schemas for every model-backed step."""

from __future__ import annotations

from typing import Any


def _string_array(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


# --------------------------------------------------------------------------
# 1. natural language -> structured search
# --------------------------------------------------------------------------

QUERY_PARSER_SYSTEM = """\
You convert a recruiter's natural-language search into a structured query for a \
people-search engine.

Extract only what the recruiter actually asked for. Leave a field empty when the \
query says nothing about it — an invented constraint silently removes good people \
from the result set, which is the most expensive error you can make here.

Conventions:
- Normalise job titles to lowercase singular form ("senior ML engineer" -> "machine learning engineer").
- Put seniority in `seniority`, never in `job_titles`. Allowed values: intern, junior, mid, senior, staff, principal, director, vp, cxo.
- `must_have_skills` is for requirements the recruiter framed as non-negotiable ("must know", "required", "strong X"). Everything else that is a skill goes in `nice_to_have_skills`.
- `keywords` is for meaningful domain terms that are not skills, titles, or companies.
- `freeform_criteria` holds qualitative requirements a keyword filter cannot express, one short phrase each ("has scaled a team from 5 to 50").
- `search_text` is a dense keyword-rich restatement of the query used for embedding retrieval. No filler words.
"""

QUERY_PARSER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "job_titles",
        "title_exclusions",
        "must_have_skills",
        "nice_to_have_skills",
        "seniority",
        "min_years_experience",
        "max_years_experience",
        "locations",
        "remote_ok",
        "industries",
        "company_stages",
        "company_sizes",
        "target_companies",
        "excluded_companies",
        "schools",
        "degrees",
        "fields_of_study",
        "keywords",
        "negative_keywords",
        "freeform_criteria",
        "search_text",
    ],
    "properties": {
        "job_titles": _string_array("Canonical job titles the person should hold."),
        "title_exclusions": _string_array("Titles that disqualify a person."),
        "must_have_skills": _string_array("Hard skill requirements."),
        "nice_to_have_skills": _string_array("Preferred but optional skills."),
        "seniority": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "intern", "junior", "mid", "senior", "staff",
                    "principal", "director", "vp", "cxo",
                ],
            },
            "description": "Acceptable seniority bands.",
        },
        "min_years_experience": {
            "type": ["number", "null"],
            "description": "Minimum total years of experience, or null.",
        },
        "max_years_experience": {
            "type": ["number", "null"],
            "description": "Maximum total years of experience, or null.",
        },
        "locations": _string_array("Cities, regions or countries the person should be in."),
        "remote_ok": {
            "type": ["boolean", "null"],
            "description": "True if the recruiter explicitly wants remote-friendly people.",
        },
        "industries": _string_array("Industries the person should have worked in."),
        "company_stages": _string_array(
            "Funding stages, e.g. seed, series a, series b, growth, public."
        ),
        "company_sizes": _string_array("Headcount buckets, e.g. 1-10, 11-50, 51-200, 5000+."),
        "target_companies": _string_array("Named companies the person should have worked at."),
        "excluded_companies": _string_array("Companies that disqualify a person."),
        "schools": _string_array("Named universities."),
        "degrees": _string_array("Degree levels, e.g. bs, ms, phd, mba."),
        "fields_of_study": _string_array("Academic fields."),
        "keywords": _string_array("Other meaningful terms to match on."),
        "negative_keywords": _string_array("Terms that should not appear."),
        "freeform_criteria": _string_array(
            "Qualitative requirements a filter cannot express, one short phrase each."
        ),
        "search_text": {
            "type": "string",
            "description": "Dense keyword restatement of the query for embedding retrieval.",
        },
    },
}


def query_parser_prompt(query: str) -> str:
    return f"Recruiter search:\n<query>\n{query.strip()}\n</query>"


# --------------------------------------------------------------------------
# 2. rerank + evidence
# --------------------------------------------------------------------------

RERANK_SYSTEM = """\
You are grading one candidate profile against one role's requirements for a recruiter.

Judge only from the profile text supplied. When the profile is silent on a \
requirement, mark it `unknown` rather than guessing — a recruiter reading your \
output will act on it, and a confident wrong call costs them a real conversation.

For every requirement, quote or closely paraphrase the specific line of the profile \
that supports your verdict. Scores: 1.0 clearly meets, 0.6 partially or adjacently \
meets, 0.0 does not meet.

`summary` is two sentences at most: why this person is or isn't a fit, in plain \
language a hiring manager would read.
"""

RERANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overall_score", "summary", "criteria", "concerns"],
    "properties": {
        "overall_score": {
            "type": "number",
            "description": "Overall fit from 0.0 to 1.0.",
        },
        "summary": {"type": "string", "description": "At most two sentences."},
        "criteria": {
            "type": "array",
            "description": "One entry per requirement supplied.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["requirement", "status", "score", "evidence"],
                "properties": {
                    "requirement": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["strong", "partial", "miss", "unknown"],
                    },
                    "score": {"type": "number"},
                    "evidence": {
                        "type": "string",
                        "description": "Quote or close paraphrase from the profile.",
                    },
                },
            },
        },
        "concerns": _string_array("Risks a recruiter should know before reaching out."),
    },
}


def rerank_prompt(*, requirements: list[str], profile: str) -> str:
    lines = "\n".join(f"- {r}" for r in requirements) or "- (no explicit requirements)"
    return (
        f"<requirements>\n{lines}\n</requirements>\n\n"
        f"<candidate_profile>\n{profile.strip()}\n</candidate_profile>"
    )


# --------------------------------------------------------------------------
# 3. outreach personalisation
# --------------------------------------------------------------------------

PERSONALIZE_SYSTEM = """\
You write the personalised fragments of a recruiting outreach email.

You are not writing the whole email — you produce short variables the recruiter's \
template drops in. Ground every fragment in something specific from the profile \
(a company, a project, a technology, a career move). Generic flattery gets ignored \
and makes the whole sequence read as automated.

Constraints:
- `opening_line`: one sentence, under 25 words, referencing something concrete.
- `why_relevant`: one sentence connecting their background to this role.
- `subject_hint`: under 8 words, no exclamation marks, not clickbait.
- Never invent employers, titles, dates, or achievements that are not in the profile.
"""

PERSONALIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["opening_line", "why_relevant", "subject_hint", "hooks"],
    "properties": {
        "opening_line": {"type": "string"},
        "why_relevant": {"type": "string"},
        "subject_hint": {"type": "string"},
        "hooks": _string_array("Concrete profile details used, for auditability."),
    },
}


def personalize_prompt(*, profile: str, role_context: str) -> str:
    return (
        f"<role>\n{role_context.strip()}\n</role>\n\n"
        f"<candidate_profile>\n{profile.strip()}\n</candidate_profile>"
    )


# --------------------------------------------------------------------------
# 4. reply classification
# --------------------------------------------------------------------------

REPLY_SYSTEM = """\
You classify a candidate's reply to a recruiting email so the sequencer knows what \
to do next.

Intents:
- interested: wants to talk, asks for details, proposes a time.
- not_interested: declines with no opening.
- not_now: open later, currently unavailable, "ask me in six months".
- referral: not for them, but points at someone else.
- question: needs information before deciding.
- out_of_office: automated absence reply.
- unsubscribe: asks to stop being contacted. Anything resembling an opt-out belongs here.
- bounce: automated delivery failure.

`should_stop_sequence` is true for every intent except out_of_office.
"""

REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "confidence", "should_stop_sequence", "suggested_next_step", "summary"],
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "interested", "not_interested", "not_now", "referral",
                "question", "out_of_office", "unsubscribe", "bounce",
            ],
        },
        "confidence": {"type": "number"},
        "should_stop_sequence": {"type": "boolean"},
        "suggested_next_step": {"type": "string"},
        "summary": {"type": "string"},
    },
}


def reply_prompt(*, subject: str, body: str) -> str:
    return f"<subject>{subject.strip()}</subject>\n<reply>\n{body.strip()}\n</reply>"
