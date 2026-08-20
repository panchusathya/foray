"""Write-side helpers shared by the API, the CLI and the ingestion workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .enrich.normalize import NormalizedCandidate, normalize_candidate
from .llm.embeddings import get_embedder
from .models import Candidate, Education, Experience, utcnow
from .search.index import invalidate


@dataclass
class UpsertResult:
    created: int = 0
    updated: int = 0
    ids: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.ids is None:
            self.ids = []


def _apply(candidate: Candidate, data: NormalizedCandidate) -> None:
    candidate.full_name = data.full_name
    candidate.first_name = data.first_name
    candidate.last_name = data.last_name
    candidate.headline = data.headline
    candidate.summary = data.summary
    candidate.location_city = data.location_city
    candidate.location_region = data.location_region
    candidate.location_country = data.location_country
    candidate.open_to_remote = data.open_to_remote
    candidate.linkedin_url = data.linkedin_url
    candidate.github_url = data.github_url
    candidate.website_url = data.website_url
    candidate.emails = data.emails
    candidate.phones = data.phones
    candidate.skills = data.skills
    candidate.skills_raw = data.skills_raw
    candidate.industries = data.industries
    candidate.current_title = data.current_title
    candidate.current_title_normalized = data.current_title_normalized
    candidate.current_company = data.current_company
    candidate.current_company_domain = data.current_company_domain
    candidate.seniority = data.seniority
    candidate.years_experience = data.years_experience
    candidate.doc_text = data.doc_text
    candidate.raw = data.raw
    candidate.enriched_at = utcnow()


def _replace_children(db: Session, candidate: Candidate, data: NormalizedCandidate) -> None:
    # delete-orphan cascade removes the detached rows on flush.
    candidate.experiences = []
    candidate.educations = []
    db.flush()

    for position, exp in enumerate(data.experiences):
        candidate.experiences.append(
            Experience(
                candidate_id=candidate.id,
                position=position,
                company=exp.company,
                company_domain=exp.company_domain,
                company_industry=exp.company_industry,
                company_size=exp.company_size,
                company_stage=exp.company_stage,
                title=exp.title,
                title_normalized=exp.title_normalized,
                seniority=exp.seniority,
                start_date=exp.start_date,
                end_date=exp.end_date,
                is_current=exp.is_current,
                months=exp.months,
                location=exp.location,
                description=exp.description,
            )
        )
    for edu in data.educations:
        candidate.educations.append(
            Education(
                candidate_id=candidate.id,
                school=edu.school,
                degree=edu.degree,
                field_of_study=edu.field_of_study,
                start_year=edu.start_year,
                end_year=edu.end_year,
            )
        )


def upsert_candidates(
    db: Session, *, org_id: str, payloads: list[dict[str, Any]], embed: bool = True
) -> UpsertResult:
    """Normalise, de-duplicate on (source, source_id), and embed in one batch."""
    result = UpsertResult()
    normalized: list[NormalizedCandidate] = []
    for payload in payloads:
        try:
            normalized.append(normalize_candidate(payload))
        except ValueError:
            continue
    if not normalized:
        return result

    keys = {(n.source, n.source_id) for n in normalized}
    existing_rows = db.scalars(
        select(Candidate).where(
            Candidate.org_id == org_id,
            Candidate.source.in_({k[0] for k in keys}),
            Candidate.source_id.in_({k[1] for k in keys}),
        )
    ).all()
    existing = {(row.source, row.source_id): row for row in existing_rows}

    touched: list[Candidate] = []
    for data in normalized:
        candidate = existing.get((data.source, data.source_id))
        if candidate is None:
            candidate = Candidate(org_id=org_id, source=data.source, source_id=data.source_id)
            _apply(candidate, data)  # populate before the INSERT, not after
            db.add(candidate)
            db.flush()
            # Register it immediately: a payload can repeat a key (two Foray
            # pages carrying the same login, a CSV with a duplicate row), and
            # without this the second copy INSERTs again and trips
            # uq_candidate_source, failing the whole batch with a 500.
            existing[(data.source, data.source_id)] = candidate
            result.created += 1
        else:
            _apply(candidate, data)
            result.updated += 1
        _replace_children(db, candidate, data)
        touched.append(candidate)
        result.ids.append(candidate.id)

    if embed and touched:
        embedder = get_embedder()
        vectors = embedder.embed([c.doc_text for c in touched])
        for candidate, vector in zip(touched, vectors, strict=True):
            candidate.embedding = [float(v) for v in vector]
            candidate.embedding_model = embedder.name

    db.flush()
    invalidate(org_id)
    return result


def reembed_org(db: Session, *, org_id: str, batch_size: int = 256) -> int:
    """Recompute every embedding — run after switching embedding providers."""
    embedder = get_embedder()
    candidates = db.scalars(select(Candidate).where(Candidate.org_id == org_id)).all()
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        vectors = embedder.embed([c.doc_text for c in batch])
        for candidate, vector in zip(batch, vectors, strict=True):
            candidate.embedding = [float(v) for v in vector]
            candidate.embedding_model = embedder.name
        db.flush()
    invalidate(org_id)
    from .models import JobPosting
    from .search.parser import ParsedQuery

    postings = db.scalars(select(JobPosting).where(JobPosting.org_id == org_id)).all()
    for start in range(0, len(postings), batch_size):
        batch = postings[start : start + batch_size]
        texts = []
        for posting in batch:
            spec = ParsedQuery.from_dict(posting.parsed_spec or {})
            texts.append(spec.retrieval_text() or posting.title or posting.jd_text or "")
        vectors = embedder.embed(texts)
        for posting, vector in zip(batch, vectors, strict=True):
            posting.embedding = [float(v) for v in vector]
            posting.embedding_model = embedder.name
        db.flush()
    return len(candidates)
