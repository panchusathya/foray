"""Warm contacts for an application, and the intro the candidate sends.

**We never send these messages.** The draft is first-person as the candidate
and is delivered to them (thread or portal) to copy, edit, and send from
their own account — sending on their behalf is a different consent posture
this deliberately does not take.

Suggestions come from ``jobmatch.referrals.warm_contacts`` (ranked, capped,
suppression-checked) and are cached on ``Application.networking`` so the
thread, the portal, and the recruiter view all read the same list.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..jobmatch.referrals import company_key, warm_contacts
from ..llm.client import get_llm
from ..llm.prompts import INTRO_DRAFT_SCHEMA, INTRO_DRAFT_SYSTEM, intro_draft_prompt
from ..models import Application, Candidate, JobPosting, utcnow

logger = logging.getLogger(__name__)

MAX_SUGGESTIONS = 5


def refresh_networking(db: Session, application: Application) -> list[dict]:
    """(Re)compute the suggestion list for one application and cache it."""
    posting = db.get(JobPosting, application.job_posting_id)
    applicant = db.scalar(
        select(Candidate)
        .where(Candidate.id == application.candidate_id)
        .options(selectinload(Candidate.experiences), selectinload(Candidate.educations))
    )
    if posting is None or applicant is None:
        return []
    contacts = warm_contacts(
        db,
        application.org_id,
        company_normalized=posting.company_normalized or "",
        company_domain=posting.company_domain or "",
        for_candidate=applicant,
        limit=MAX_SUGGESTIONS,
    )
    # Drafts already written for a contact survive a refresh.
    drafted = {
        item.get("candidate_id"): item
        for item in (application.networking or [])
        if item.get("draft")
    }
    cached = []
    for contact in contacts:
        existing = drafted.get(contact["candidate_id"]) or {}
        cached.append(
            {
                **contact,
                "draft": existing.get("draft", ""),
                "drafted_at": existing.get("drafted_at", ""),
                "generated_by": existing.get("generated_by", ""),
            }
        )
    application.networking = cached
    db.flush()
    return cached


def draft_intro(db: Session, application: Application, contact_candidate_id: str) -> dict:
    """Write (or rewrite) the intro for one suggested contact. Returns the
    updated networking entry; raises ``ValueError`` for a contact not on the
    application's list — drafting to an arbitrary person is not a feature."""
    entries = list(application.networking or [])
    index = next(
        (i for i, item in enumerate(entries) if item.get("candidate_id") == contact_candidate_id),
        None,
    )
    if index is None:
        raise ValueError("that person is not on this application's suggestion list")

    applicant = db.scalar(
        select(Candidate)
        .where(Candidate.id == application.candidate_id)
        .options(selectinload(Candidate.experiences), selectinload(Candidate.educations))
    )
    contact = db.get(Candidate, contact_candidate_id)
    posting = db.get(JobPosting, application.job_posting_id)
    if applicant is None or contact is None or posting is None:
        raise ValueError("application lost its candidate, contact, or posting")

    body, generated_by = _compose_intro(applicant, contact, posting, entries[index])
    entries[index] = {
        **entries[index],
        "draft": body,
        "drafted_at": utcnow().isoformat(),
        "generated_by": generated_by,
    }
    # Reassigned, never mutated: JSON columns do not track in-place changes.
    application.networking = entries
    db.flush()
    return entries[index]


def _compose_intro(
    applicant: Candidate, contact: Candidate, posting: JobPosting, entry: dict
) -> tuple[str, str]:
    connection = _connection_point(applicant, contact, entry)
    llm = get_llm()
    if llm.available:
        try:
            result = llm.json(
                system=INTRO_DRAFT_SYSTEM,
                prompt=intro_draft_prompt(
                    applicant_context=_person_line(applicant),
                    contact_context=_person_line(contact),
                    role_line=f"{posting.title} at {posting.company}",
                    connection_point=connection,
                ),
                schema=INTRO_DRAFT_SCHEMA,
            )
            body = str((result.data or {}).get("body") or "").strip()
            if body:
                return body, "llm"
        except Exception as exc:  # noqa: BLE001 - the template ships instead
            logger.warning("intro draft failed, using template: %s", exc)
    return _template_intro(applicant, contact, posting, connection), "template"


def _template_intro(
    applicant: Candidate, contact: Candidate, posting: JobPosting, connection: str
) -> str:
    first = (contact.full_name or "").split(" ")[0] or "there"
    opener = f"Hi {first} — {connection}." if connection else f"Hi {first} —"
    return (
        f"{opener} I've just applied for the {posting.title} role at "
        f"{posting.company} and would value any advice about the team before "
        "the process gets going. Would you have ten minutes for a quick chat?"
    )


def _connection_point(applicant: Candidate, contact: Candidate, entry: dict) -> str:
    applicant_companies = {
        company_key(exp.company): exp.company
        for exp in (applicant.experiences or [])
        if exp.company
    }
    for exp in contact.experiences or []:
        key = company_key(exp.company)
        if key and key in applicant_companies:
            return f"we both spent time at {applicant_companies[key]}"
    applicant_schools = {
        (edu.school or "").strip().casefold(): edu.school
        for edu in (applicant.educations or [])
        if edu.school
    }
    for edu in contact.educations or []:
        key = (edu.school or "").strip().casefold()
        if key and key in applicant_schools:
            return f"we both went to {applicant_schools[key]}"
    if entry.get("basis") == "alumni":
        return f"I saw you used to be at {entry.get('company') or 'the company'}"
    return ""


def _person_line(person: Candidate) -> str:
    bits = [person.full_name or ""]
    if person.current_title:
        bits.append(person.current_title)
    if person.current_company:
        bits.append(f"at {person.current_company}")
    return " — ".join(bit for bit in bits if bit)
