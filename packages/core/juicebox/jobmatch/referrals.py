"""Who in the CRM works at (or used to work at) this company.

Blank ``company_normalized`` is "no company": no paths, no incumbent
exclusions. Incumbents are referrers, not candidates for the posting.

Two callers, one engine:

* ``warm_paths(db, org_id, posting)`` — the original, cached onto
  ``JobPosting.referral_paths``. Output shape unchanged (regression-pinned).
* ``warm_contacts(...)`` — the candidate-agent generalisation: any company,
  ranked, capped, and filtered through the do-not-contact gates, because a
  suggestion list that includes somebody who opted out is a suggestion to
  break a promise.

Ranking is deterministic — current beats alumni, seniority helps, shared
history with the applicant helps, staleness hurts — and prefiltered in SQL on
the normalized-company columns rather than loading the org.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from ..enrich import taxonomy as tx
from ..models import Candidate, Experience, JobPosting, utcnow
from ..outreach.suppressions import is_suppressed

# Seniority worth a boost: people who can actually carry a referral.
_SENIOR_RANKS = {"staff", "principal", "director", "vp", "cxo", "founder", "lead", "senior"}
_STALE_AFTER = timedelta(days=540)  # ~18 months since last enrichment


def company_key(value: str) -> str:
    return tx.normalize_company(value or "")


def has_company(posting: JobPosting) -> bool:
    return bool((posting.company_normalized or "").strip())


def is_incumbent(candidate: Candidate, posting: JobPosting) -> bool:
    if not has_company(posting):
        return False
    current = company_key(candidate.current_company)
    return bool(current) and current == posting.company_normalized


def warm_paths(db: Session, org_id: str, posting: JobPosting) -> list[dict]:
    """Return [{candidate_id, name, company, basis}] for this posting.

    The original contract, kept byte-compatible: ``JobPosting.referral_paths``
    and its UI render exactly these keys.
    """
    if not has_company(posting):
        return []
    contacts = warm_contacts(
        db,
        org_id,
        company_normalized=posting.company_normalized,
        company_domain=posting.company_domain or "",
        limit=0,
        check_suppression=False,
    )
    return [
        {
            "candidate_id": item["candidate_id"],
            "name": item["name"],
            "company": item["company"],
            "basis": item["basis"],
        }
        for item in contacts
    ]


def warm_contacts(
    db: Session,
    org_id: str,
    *,
    company_normalized: str,
    company_domain: str = "",
    for_candidate: Candidate | None = None,
    limit: int = 10,
    check_suppression: bool = True,
) -> list[dict]:
    """Ranked people worth talking to at one company.

    ``for_candidate`` is the applicant: excluded from their own list, and
    shared past employers / schools with them raise a contact's rank —
    "you both worked at Stripe" is a warmer opener than "they work there".
    ``limit=0`` means uncapped (the ``warm_paths`` cache case).
    """
    target = (company_normalized or "").strip()
    domain = (company_domain or "").strip().lower()
    if not target and not domain:
        return []

    matched = _prefilter(db, org_id, target=target, domain=domain)

    applicant_companies: set[str] = set()
    applicant_schools: set[str] = set()
    if for_candidate is not None:
        applicant_companies = {
            company_key(exp.company)
            for exp in (for_candidate.experiences or [])
            if exp.company
        }
        applicant_companies.discard("")
        applicant_companies.discard(target)
        applicant_schools = {
            (edu.school or "").strip().casefold()
            for edu in (for_candidate.educations or [])
            if edu.school
        }
        applicant_schools.discard("")

    now = utcnow()
    ranked: list[tuple[float, dict]] = []
    for candidate in matched:
        if for_candidate is not None and candidate.id == for_candidate.id:
            continue
        basis = _basis(candidate, target, domain)
        if not basis:
            continue
        if candidate.crm_status == "do_not_contact":
            continue
        if check_suppression and _is_unreachable(db, candidate):
            continue
        score = _rank(
            candidate,
            basis=basis,
            applicant_companies=applicant_companies,
            applicant_schools=applicant_schools,
            now=now,
        )
        ranked.append(
            (
                score,
                {
                    "candidate_id": candidate.id,
                    "name": candidate.full_name,
                    "title": candidate.current_title or "",
                    "company": candidate.current_company
                    or _matching_experience_company(candidate, target),
                    "basis": basis,
                    "score": round(score, 3),
                    "linkedin_url": candidate.linkedin_url or "",
                },
            )
        )

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    contacts = [item for _, item in ranked]
    return contacts[:limit] if limit else contacts


def _prefilter(db: Session, org_id: str, *, target: str, domain: str) -> list[Candidate]:
    """SQL-first: current company match, unioned with an experience match."""
    current_terms = []
    if target:
        current_terms.append(Candidate.current_company_normalized == target)
    if domain:
        current_terms.append(Candidate.current_company_domain == domain)

    experience_terms = []
    if target:
        experience_terms.append(Experience.company_normalized == target)
    if domain:
        experience_terms.append(Experience.company_domain == domain)

    ids = set(
        db.scalars(
            select(Candidate.id).where(Candidate.org_id == org_id, or_(*current_terms))
        ).all()
    )
    ids.update(
        db.scalars(
            select(Experience.candidate_id)
            .join(Candidate, Candidate.id == Experience.candidate_id)
            .where(Candidate.org_id == org_id, or_(*experience_terms))
        ).all()
    )
    if not ids:
        return []
    return list(
        db.scalars(
            select(Candidate)
            .where(Candidate.id.in_(ids))
            .options(selectinload(Candidate.experiences), selectinload(Candidate.educations))
            .execution_options(populate_existing=True)
        ).all()
    )


def _rank(
    candidate: Candidate,
    *,
    basis: str,
    applicant_companies: set[str],
    applicant_schools: set[str],
    now: datetime,
) -> float:
    score = 1.0 if basis == "current" else 0.6

    seniority = (candidate.seniority or "").strip().lower()
    if seniority in _SENIOR_RANKS:
        score += 0.15

    if applicant_companies:
        shared = {
            company_key(exp.company) for exp in (candidate.experiences or []) if exp.company
        }
        overlap = len(shared & applicant_companies)
        score += min(overlap, 2) * 0.2

    if applicant_schools:
        schools = {
            (edu.school or "").strip().casefold()
            for edu in (candidate.educations or [])
            if edu.school
        }
        if schools & applicant_schools:
            score += 0.15

    enriched = candidate.enriched_at
    if enriched is not None:
        if enriched.tzinfo is None:
            enriched = enriched.replace(tzinfo=UTC)
        if now - enriched > _STALE_AFTER:
            score -= 0.1

    return score


def _is_unreachable(db: Session, candidate: Candidate) -> bool:
    """On the suppression list by any address we hold = not a suggestion."""
    for email in candidate.emails or []:
        if isinstance(email, str) and email.strip() and is_suppressed(
            db, org_id=candidate.org_id, email=email
        ):
            return True
    for phone in candidate.phones or []:
        if isinstance(phone, str) and phone.strip() and is_suppressed(
            db, org_id=candidate.org_id, phone=phone
        ):
            return True
    return False


def _basis(candidate: Candidate, target: str, domain: str) -> str:
    current = company_key(candidate.current_company)
    if target and current and current == target:
        return "current"
    cand_domain = (candidate.current_company_domain or "").strip().lower()
    if domain and cand_domain and cand_domain == domain:
        return "current"
    for exp in candidate.experiences:
        if exp.is_current:
            continue
        if target and company_key(exp.company) == target:
            return "alumni"
        exp_domain = (exp.company_domain or "").strip().lower()
        if domain and exp_domain and exp_domain == domain:
            return "alumni"
    return ""


def _matching_experience_company(candidate: Candidate, target: str) -> str:
    for exp in candidate.experiences:
        if company_key(exp.company) == target:
            return exp.company
    return candidate.current_company or ""
