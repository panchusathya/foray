"""Promoting parsed facts onto a Candidate without destroying anything.

The two hazards this module exists to prevent (CANDIDATE_AGENT_PLAN §7):

* ``repository._replace_children`` rebuilds experience/education rows
  wholesale from its payload — a raw re-upsert of a *parsed* profile would
  delete corrections a human already made.
* ``Candidate.summary`` and ``.skills`` feed the search haystack — a resume
  parse silently changing them changes what recruiters find.

So parsed facts (a resume's ``resume.v1``, a LinkedIn profile) land on
``CandidateDocument.structured`` first, and promotion is THIS explicit step:
build a **merged** payload — everything the candidate already has, plus only
what is genuinely new — and run it through the ordinary ``upsert_candidates``
machinery, which keeps normalization, ``manual_fields`` immunity, doc_text and
embedding all in the one place they already live. The rebuild is safe because
the merged list is a superset of the existing rows by construction.

Rules, in one place:

* Scalars fill blanks only. A parsed value never replaces a present one
  (``force=True`` is the recruiter override, and even it never touches
  summary/skills).
* Experiences/educations are additive, keyed on (normalized company, title) /
  school. Nothing is deleted, nothing present is rewritten.
* Skills and summary move only when the candidate has none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..models import Candidate
from .taxonomy import normalize_company, normalize_title

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromoteResult:
    candidate_id: str
    created: bool
    added_experiences: int = 0
    added_educations: int = 0
    filled_fields: list[str] = field(default_factory=list)


def payload_from_resume_v1(
    structured: dict,
    *,
    fallback_name: str = "",
    source: str = "resume_upload",
    source_id: str = "",
) -> dict:
    """``resume.v1`` → the ``upsert_candidates`` payload shape."""
    contact = structured.get("contact") or {}
    name = str(contact.get("name") or "").strip() or fallback_name

    experiences = []
    for job in structured.get("experience") or []:
        if not isinstance(job, dict):
            continue
        end = str(job.get("end") or "").strip()
        is_current = end.lower() in {"present", "current", "now"}
        experiences.append(
            {
                "company": str(job.get("company") or ""),
                "title": str(job.get("title") or ""),
                "start_date": str(job.get("start") or "")[:7],
                "end_date": "" if is_current else end[:7],
                "is_current": is_current,
                "location": str(job.get("location") or ""),
                "description": "\n".join(str(b) for b in job.get("bullets") or []),
            }
        )

    educations = []
    for edu in structured.get("education") or []:
        if not isinstance(edu, dict):
            continue
        educations.append(
            {
                "school": str(edu.get("school") or ""),
                "degree": str(edu.get("degree") or ""),
                "field_of_study": str(edu.get("field") or ""),
                "start_year": _year(edu.get("start_year")),
                "end_year": _year(edu.get("end_year")),
            }
        )

    payload: dict = {
        "source": source,
        "source_id": source_id or name.lower().replace(" ", "-") or "unknown",
        "full_name": name,
        "summary": str(structured.get("summary") or ""),
        "linkedin_url": str(contact.get("linkedin_url") or ""),
        "github_url": str(contact.get("github_url") or ""),
        "website_url": str(contact.get("website_url") or ""),
        "skills": [str(s) for s in structured.get("skills") or [] if str(s).strip()],
        "experiences": experiences,
        "educations": educations,
    }
    email = str(contact.get("email") or "").strip()
    if email:
        payload["emails"] = [email]
    phone = str(contact.get("phone") or "").strip()
    if phone:
        payload["phones"] = [phone]
    location = str(contact.get("location") or "").strip()
    if location:
        payload["location_city"] = location
    return payload


def _year(value) -> int | None:
    try:
        year = int(str(value).strip()[:4])
    except (TypeError, ValueError):
        return None
    return year if 1900 < year < 2100 else None


def promote(
    db: Session,
    *,
    org_id: str,
    parsed: dict,
    candidate: Candidate | None = None,
    force: bool = False,
) -> PromoteResult:
    """Apply one parsed payload (upsert shape) to the CRM, additively.

    ``candidate=None`` is the stranger case — nothing exists to protect, so
    the payload lands whole through the ordinary upsert. With an existing
    candidate the payload is first merged down to "only what is new".
    """
    from ..repository import upsert_candidates
    from ..search.index import invalidate

    if candidate is None:
        result = upsert_candidates(db, org_id=org_id, payloads=[parsed])
        candidate_id = result.ids[0] if result.ids else ""
        return PromoteResult(candidate_id=candidate_id, created=True)

    merged, stats = merged_payload(candidate, parsed, force=force)
    if merged is None:
        return PromoteResult(candidate_id=candidate.id, created=False)
    upsert_candidates(db, org_id=org_id, payloads=[merged])
    invalidate(org_id)
    return PromoteResult(
        candidate_id=candidate.id,
        created=False,
        added_experiences=stats["added_experiences"],
        added_educations=stats["added_educations"],
        filled_fields=stats["filled_fields"],
    )


def merged_payload(
    candidate: Candidate, parsed: dict, *, force: bool = False
) -> tuple[dict | None, dict]:
    """Everything the candidate has, plus only what the parse adds.

    Returns ``(payload, stats)``; payload is ``None`` when the parse adds
    nothing at all, so callers can skip a pointless re-embed.
    """
    stats = {"added_experiences": 0, "added_educations": 0, "filled_fields": []}

    payload: dict = {
        # The candidate's own identity keys, so the upsert updates in place.
        "source": candidate.source,
        "source_id": candidate.source_id,
        "full_name": candidate.full_name,
        "first_name": candidate.first_name,
        "last_name": candidate.last_name,
        "headline": candidate.headline,
        # Haystack fields: ALWAYS the existing values unless the candidate has
        # none. force does not override this pair -- that is the §7 rule.
        "summary": candidate.summary or str(parsed.get("summary") or ""),
        "location_city": candidate.location_city,
        "location_region": candidate.location_region,
        "location_country": candidate.location_country,
        "open_to_remote": candidate.open_to_remote,
        "linkedin_url": candidate.linkedin_url,
        "github_url": candidate.github_url,
        "website_url": candidate.website_url,
        "emails": list(candidate.emails or []),
        "phones": list(candidate.phones or []),
        "raw": dict(candidate.raw or {}),
    }
    if candidate.skills:
        payload["skills"] = list(candidate.skills)
    else:
        parsed_skills = parsed.get("skills") or []
        if parsed_skills:
            payload["skills"] = list(parsed_skills)
            stats["filled_fields"].append("skills")
    if not candidate.summary and payload["summary"]:
        stats["filled_fields"].append("summary")

    # Scalar fill-blanks (or force-replace, never for summary/skills).
    for key in (
        "full_name",
        "headline",
        "location_city",
        "location_region",
        "location_country",
        "linkedin_url",
        "github_url",
        "website_url",
    ):
        incoming = str(parsed.get(key) or "").strip()
        if not incoming:
            continue
        current = str(payload.get(key) or "").strip()
        if not current or (force and incoming != current):
            payload[key] = incoming
            stats["filled_fields"].append(key)

    for key in ("emails", "phones"):
        existing = [v for v in payload[key] if isinstance(v, str) and v.strip()]
        seen = {v.strip().lower() for v in existing}
        for value in parsed.get(key) or []:
            if isinstance(value, str) and value.strip() and value.strip().lower() not in seen:
                existing.append(value.strip())
                seen.add(value.strip().lower())
                stats["filled_fields"].append(key)
        payload[key] = existing

    # Children: existing rows first (verbatim), then genuinely new ones.
    existing_experiences = [
        {
            "company": exp.company,
            "company_domain": exp.company_domain,
            "company_industry": exp.company_industry,
            "company_size": exp.company_size,
            "company_stage": exp.company_stage,
            "title": exp.title,
            "start_date": exp.start_date,
            "end_date": exp.end_date,
            "is_current": exp.is_current,
            "location": exp.location,
            "description": exp.description,
        }
        for exp in sorted(candidate.experiences or [], key=lambda e: e.position)
    ]
    known = {
        (normalize_company(exp["company"]), normalize_title(exp["title"]))
        for exp in existing_experiences
    }
    merged_experiences = list(existing_experiences)
    for exp in parsed.get("experiences") or []:
        key = (
            normalize_company(str(exp.get("company") or "")),
            normalize_title(str(exp.get("title") or "")),
        )
        if not key[0] or key in known:
            continue
        known.add(key)
        merged_experiences.append(exp)
        stats["added_experiences"] += 1
    payload["experiences"] = merged_experiences

    existing_educations = [
        {
            "school": edu.school,
            "degree": edu.degree,
            "field_of_study": edu.field_of_study,
            "start_year": edu.start_year,
            "end_year": edu.end_year,
        }
        for edu in candidate.educations or []
    ]
    schools = {(edu["school"] or "").strip().casefold() for edu in existing_educations}
    merged_educations = list(existing_educations)
    for edu in parsed.get("educations") or []:
        school = str(edu.get("school") or "").strip().casefold()
        if not school or school in schools:
            continue
        schools.add(school)
        merged_educations.append(edu)
        stats["added_educations"] += 1
    payload["educations"] = merged_educations

    changed = (
        stats["added_experiences"]
        or stats["added_educations"]
        or stats["filled_fields"]
    )
    return (payload if changed else None), stats
