"""Build the ``resume.v1`` JSON a render or a tailor starts from.

The deterministic path is the contract: given only the Candidate row, its
Experience and Education children, and the intake preference, the output is a
complete, truthful resume. The LLM path receives that JSON and may reorder,
tighten, and cut — the tailor prompt forbids invention, and the adversarial
test hands it a transport that tries anyway and asserts the guard catches it.

Between the two sits ``docgen/bullets``, which fills in roles LinkedIn handed
over with no description using what the candidate said about that work during
intake. It is on the LLM side of the line and guarded the same way: a bullet
that cannot be traced verbatim to the candidate's own words is discarded, so
the worst case is the bare resume this module already built.
"""

from __future__ import annotations

import copy
import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..llm.client import get_llm
from ..llm.prompts import (
    RESUME_TAILOR_SCHEMA,
    RESUME_TAILOR_SYSTEM,
    resume_tailor_prompt,
)
from ..models import Candidate, CandidateDocument, CandidatePreference, JobPosting
from ..search.parser import ParsedQuery
from .bullets import candidate_evidence, fill_missing_bullets, remembered_bullets

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
    """``(resume.v1 dict, generated_by)`` where generated_by is upload|llm|template.

    Tailoring without a model is real: skills are reordered by overlap with
    the posting's parsed spec, which is most of what "tailored" means at the
    keyword-screen stage.
    A candidate's default upload is the source of truth. When its parse has a
    usable ``resume.v1`` object, return that object without adding, deleting,
    or rewriting facts from LinkedIn/CRM data. The original uploaded file is
    attached by ``applications.service``; this JSON supports answers and
    cover letters only.
    """
    uploaded = default_uploaded_resume(db, candidate.id)
    structured = uploaded.structured if uploaded is not None else None
    if isinstance(structured, dict) and structured:
        return _normalized_uploaded_resume(structured), "upload"

    base = _deterministic_resume(candidate, preference=preference, posting=posting)
    _graft_linkedin_sections(db, candidate, base)

    # A LinkedIn history is usually employers and dates with nothing written
    # underneath, because almost nobody fills in the description box. What the
    # candidate said on their intake call describes that same work, so
    # ``bullets`` moves it onto the page — discarding anything it cannot trace
    # back to their own words. It runs BEFORE the tailor so the tailor has
    # bullets to order against the role, and what it writes is credited to the
    # model: a drafted bullet is not something the deterministic path produced.
    #
    # This function runs once per application, so the previously drafted
    # bullets go in first: one account of a job, reused, rather than a fresh
    # one composed for every employer who reads it.
    base, drafted = fill_missing_bullets(
        base,
        evidence=candidate_evidence(db, candidate),
        remembered=remembered_bullets(db, candidate),
    )
    bare = "llm" if drafted else "template"

    llm = get_llm()
    if not llm.available:
        return base, bare
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
        return base, bare

    tailored = result.data or {}
    if not isinstance(tailored, dict) or not _tailor_is_honest(base, tailored):
        logger.warning("resume tailor rejected: output invented facts or lost shape")
        return base, bare
    return tailored, "llm"


def default_uploaded_resume(db: Session, candidate_id: str) -> CandidateDocument | None:
    """Return the candidate-authored resume that supersedes every other source."""
    return db.scalar(
        select(CandidateDocument)
        .where(
            CandidateDocument.candidate_id == candidate_id,
            CandidateDocument.kind == "resume_uploaded",
            CandidateDocument.is_default.is_(True),
        )
        .order_by(CandidateDocument.created_at.desc())
    )


def _normalized_uploaded_resume(structured: dict) -> dict:
    """Copy an upload and add only absent schema containers, never new facts."""
    resume = copy.deepcopy(structured)
    resume.setdefault("contact", {})
    resume.setdefault("summary", "")
    for key in (
        "experience",
        "education",
        "skills",
        "certifications",
        "languages",
        "leadership",
        "interests",
    ):
        resume.setdefault(key, [])
    return resume


def _graft_linkedin_sections(db: Session, candidate: Candidate, base: dict) -> None:
    """Use LinkedIn-only leadership when no candidate-authored resume exists."""
    profile = db.scalar(
        select(CandidateDocument)
        .where(
            CandidateDocument.candidate_id == candidate.id,
            CandidateDocument.source == "linkedin_ingest",
        )
        .order_by(CandidateDocument.created_at.desc())
    )
    profile_structured = profile.structured if profile is not None else None
    structured = None
    if isinstance(profile_structured, dict):
        structured = {
            "leadership": ((profile_structured.get("raw") or {}).get("leadership") or [])
        }
    if not isinstance(structured, dict) or not structured:
        return

    if not base.get("leadership") and structured.get("leadership"):
        base["leadership"] = list(structured["leadership"])


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
        # LinkedIn providers preserve authored bullets in a few shapes: real
        # line breaks, or one text block separated by bullet glyphs. Split
        # those markers so every real profile bullet reaches the resume while
        # leaving ordinary prose as one truthful bullet.
        bullets = [
            line.strip(" -•●▪\t")
            for line in re.split(r"[\r\n]+|[•●▪]", exp.description or "")
            if line.strip(" -•●▪\t")
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
        # LinkedIn source artifacts can fill leadership when there is no
        # uploaded resume. Empty is honest here.
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
