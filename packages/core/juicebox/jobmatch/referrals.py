"""Who in the CRM works at (or used to work at) this company.

Blank ``company_normalized`` is "no company": no paths, no incumbent
exclusions. Incumbents are referrers, not candidates for the posting.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..enrich import taxonomy as tx
from ..models import Candidate, JobPosting


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
    """Return [{candidate_id, name, company, basis}] for this posting."""
    if not has_company(posting):
        return []
    target = posting.company_normalized
    domain = (posting.company_domain or "").strip().lower()
    candidates = db.scalars(
        select(Candidate)
        .where(Candidate.org_id == org_id)
        .options(selectinload(Candidate.experiences))
        .execution_options(populate_existing=True)
    ).all()
    paths: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates:
        basis = _basis(candidate, target, domain)
        if not basis or candidate.id in seen:
            continue
        seen.add(candidate.id)
        paths.append(
            {
                "candidate_id": candidate.id,
                "name": candidate.full_name,
                "company": candidate.current_company or _matching_experience_company(candidate, target),
                "basis": basis,
            }
        )
    return paths


def _basis(candidate: Candidate, target: str, domain: str) -> str:
    current = company_key(candidate.current_company)
    if current and current == target:
        return "current"
    cand_domain = (candidate.current_company_domain or "").strip().lower()
    if domain and cand_domain and cand_domain == domain:
        return "current"
    for exp in candidate.experiences:
        if exp.is_current:
            continue
        if company_key(exp.company) == target:
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
