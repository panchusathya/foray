"""Build the ``resume.v1`` JSON a render or a tailor starts from.

The deterministic path is the contract: given only the Candidate row, its
Experience and Education children, and the intake preference, the output is a
complete, truthful resume. The LLM path receives that JSON and may reorder,
tighten, and cut — the tailor prompt forbids invention, and the adversarial
test hands it a transport that tries anyway and asserts the guard catches it.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..llm.client import get_llm
from ..llm.prompts import (
    RESUME_TAILOR_SCHEMA,
    RESUME_TAILOR_SYSTEM,
    resume_tailor_prompt,
)
from ..models import Candidate, CandidatePreference, JobPosting
from ..search.parser import ParsedQuery

logger = logging.getLogger(__name__)

RESUME_SCHEMA_VERSION = "resume.v1"


def build_resume_json(
    db: Session,
    candidate: Candidate,
    *,
    preference: CandidatePreference | None = None,
    posting: JobPosting | None = None,
    uploaded_text: str = "",
) -> tuple[dict, str]:
    """``(resume.v1 dict, generated_by)`` where generated_by is llm|template.

    Tailoring without a model is real: skills are reordered by overlap with
    the posting's parsed spec, which is most of what "tailored" means at the
    keyword-screen stage.
    """
    base = _deterministic_resume(candidate, preference=preference, posting=posting)
    _graft_uploaded_sections(db, candidate, base)

    llm = get_llm()
    if not llm.available:
        return base, "template"
    try:
        result = llm.json(
            system=RESUME_TAILOR_SYSTEM,
            prompt=resume_tailor_prompt(
                resume_json=json.dumps(base, ensure_ascii=False),
                role_spec=_role_spec_text(posting),
                uploaded_text=uploaded_text,
            ),
            schema=RESUME_TAILOR_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - the deterministic resume ships instead
        logger.warning("resume tailor failed, using the deterministic resume: %s", exc)
        return base, "template"

    tailored = result.data or {}
    if not isinstance(tailored, dict) or not _tailor_is_honest(base, tailored):
        logger.warning("resume tailor rejected: output invented facts or lost shape")
        return base, "template"
    return tailored, "llm"


def _graft_uploaded_sections(db: Session, candidate: Candidate, base: dict) -> None:
    """Carry over what the CRM has no columns for, from their own upload.

    Leadership, interests, languages, certifications and the labeled lines
    under a school (Honors, GPA, Coursework) exist only on the parsed
    ``resume.v1`` of the candidate's uploaded resume — the Candidate row has
    nowhere to hold them. A generated resume that silently dropped somebody's
    licenses or campus leadership would be a worse document than the one they
    gave us. Sections graft only into blanks; nothing the deterministic base
    asserts is replaced.
    """
    from ..models import CandidateDocument

    uploaded = db.scalar(
        select(CandidateDocument)
        .where(
            CandidateDocument.candidate_id == candidate.id,
            CandidateDocument.kind == "resume_uploaded",
            CandidateDocument.is_default.is_(True),
        )
        .order_by(CandidateDocument.created_at.desc())
    )
    structured = uploaded.structured if uploaded is not None else None
    if not isinstance(structured, dict) or not structured:
        return

    for key in ("leadership", "interests", "languages", "certifications"):
        if not base.get(key) and structured.get(key):
            base[key] = list(structured[key])

    details_by_school = {
        str(edu.get("school") or "").strip().casefold(): [
            str(line) for line in edu.get("details") or []
        ]
        for edu in structured.get("education") or []
        if isinstance(edu, dict)
    }
    for edu in base.get("education") or []:
        if edu.get("details"):
            continue
        lines = details_by_school.get(str(edu.get("school") or "").strip().casefold())
        if lines:
            edu["details"] = lines


def _tailor_is_honest(base: dict, tailored: dict) -> bool:
    """The invention guard. Every employer, school, and credential in the
    output must exist in the input — reordering and cutting are the model's
    only powers over facts, and a model that adds one loses the job."""

    def _casefold_set(items: list, key: str) -> set[str]:
        out = set()
        for item in items or []:
            if isinstance(item, dict):
                value = str(item.get(key) or "").strip().casefold()
                if value:
                    out.add(value)
        return out

    base_companies = _casefold_set(base.get("experience") or [], "company")
    for company in _casefold_set(tailored.get("experience") or [], "company"):
        if company not in base_companies:
            return False

    base_schools = _casefold_set(base.get("education") or [], "school")
    for school in _casefold_set(tailored.get("education") or [], "school"):
        if school not in base_schools:
            return False

    base_orgs = _casefold_set(base.get("leadership") or [], "company")
    for org in _casefold_set(tailored.get("leadership") or [], "company"):
        if org not in base_orgs:
            return False

    base_certs = {str(c).strip().casefold() for c in base.get("certifications") or []}
    for cert in tailored.get("certifications") or []:
        if str(cert).strip().casefold() not in base_certs:
            return False

    base_skills = {str(s).strip().casefold() for s in base.get("skills") or []}
    for skill in tailored.get("skills") or []:
        if str(skill).strip().casefold() not in base_skills:
            return False

    return bool(tailored.get("contact"))


def _deterministic_resume(
    candidate: Candidate,
    *,
    preference: CandidatePreference | None,
    posting: JobPosting | None,
) -> dict:
    emails = [e for e in (candidate.emails or []) if isinstance(e, str) and e.strip()]
    phones = [p for p in (candidate.phones or []) if isinstance(p, str) and p.strip()]
    location = ", ".join(
        part
        for part in (candidate.location_city, candidate.location_region, candidate.location_country)
        if part
    )

    experience = []
    ordered = sorted(
        candidate.experiences or [],
        key=lambda e: ((0 if e.is_current else 1), e.position),
    )
    for exp in ordered:
        bullets = [
            line.strip(" -•\t")
            for line in (exp.description or "").splitlines()
            if line.strip(" -•\t")
        ]
        experience.append(
            {
                "company": exp.company or "",
                "title": exp.title or "",
                "start": exp.start_date or "",
                "end": "present" if exp.is_current else (exp.end_date or ""),
                "location": exp.location or "",
                "bullets": bullets,
            }
        )

    education = [
        {
            "school": edu.school or "",
            "degree": edu.degree or "",
            "field": edu.field_of_study or "",
            "start_year": str(edu.start_year or ""),
            "end_year": str(edu.end_year or ""),
            "details": [],
        }
        for edu in (candidate.educations or [])
    ]

    skills = [s for s in (candidate.skills or []) if isinstance(s, str) and s.strip()]
    if posting is not None:
        skills = _reorder_skills(skills, posting)

    summary = (candidate.summary or candidate.headline or "").strip()
    if not summary and preference is not None and preference.highlights:
        summary = "; ".join(str(h) for h in preference.highlights if h)[:400]

    return {
        "contact": {
            "name": candidate.full_name or "",
            "email": emails[0] if emails else "",
            "phone": phones[0] if phones else "",
            "location": location,
            "linkedin_url": candidate.linkedin_url or "",
            "github_url": candidate.github_url or "",
            "website_url": candidate.website_url or "",
        },
        "summary": summary,
        "experience": experience,
        "education": education,
        "skills": skills,
        "certifications": [],
        # The Candidate row has no columns for these; the candidate's own
        # uploaded resume fills them via _graft_uploaded_sections. Empty is
        # honest here.
        "languages": [],
        "leadership": [],
        "interests": [],
    }


def _reorder_skills(skills: list[str], posting: JobPosting) -> list[str]:
    """What the role asks for first. Tailoring with no model — and exactly the
    ordering an ATS keyword screen rewards."""
    spec = ParsedQuery.from_dict(posting.parsed_spec or {})
    wanted = [s.casefold() for s in (*spec.must_have_skills, *spec.nice_to_have_skills)]
    if not wanted:
        return skills

    def rank(skill: str) -> tuple[int, int]:
        folded = skill.casefold()
        for position, target in enumerate(wanted):
            if target and (target in folded or folded in target):
                return (0, position)
        return (1, 0)

    return sorted(skills, key=rank)


def _role_spec_text(posting: JobPosting | None) -> str:
    if posting is None:
        return "(no specific role; a general-purpose resume)"
    spec = ParsedQuery.from_dict(posting.parsed_spec or {})
    lines = [f"{posting.title} at {posting.company}".strip()]
    if spec.must_have_skills:
        lines.append("must have: " + ", ".join(spec.must_have_skills))
    if spec.nice_to_have_skills:
        lines.append("nice to have: " + ", ".join(spec.nice_to_have_skills))
    if spec.seniority:
        lines.append("seniority: " + ", ".join(spec.seniority))
    if spec.locations:
        lines.append("location: " + ", ".join(spec.locations))
    return "\n".join(line for line in lines if line)
