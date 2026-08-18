"""The searchable corpus: a per-org snapshot of candidates ready for retrieval.

Held in process and rebuilt when the underlying rows change (detected via row
count + latest ``updated_at``). At the scale this ships for, rebuilding is
cheap; the interface is the seam where you would drop in pgvector/OpenSearch.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..enrich import taxonomy as tx
from ..llm.embeddings import get_embedder, tokenize
from ..models import Candidate
from .bm25 import BM25Index


@dataclass
class CandidateDoc:
    """A flat, session-detached view of one candidate used by scoring."""

    id: str
    full_name: str
    headline: str
    summary: str
    current_title: str
    current_title_normalized: str
    current_company: str
    current_company_normalized: str
    seniority: str
    years_experience: float
    skills: set[str]
    industries: set[str]
    location_parts: list[str]
    location_city: str
    location_region: str
    location_country: str
    open_to_remote: bool
    emails: list[str]
    linkedin_url: str
    doc_text: str
    haystack: str
    titles_held: list[str] = field(default_factory=list)
    companies: list[dict[str, str]] = field(default_factory=list)
    schools: list[str] = field(default_factory=list)
    degrees: list[str] = field(default_factory=list)
    fields_of_study: list[str] = field(default_factory=list)
    updated_at: datetime | None = None

    def profile_text(self, *, max_experiences: int = 6) -> str:
        """Compact rendering handed to the model during reranking."""
        lines = [f"Name: {self.full_name}"]
        if self.headline:
            lines.append(f"Headline: {self.headline}")
        location = ", ".join(p for p in self.location_parts if p)
        if location:
            lines.append(f"Location: {location}{' (open to remote)' if self.open_to_remote else ''}")
        lines.append(f"Seniority: {self.seniority} | Total experience: {self.years_experience} yrs")
        if self.skills:
            lines.append(f"Skills: {', '.join(sorted(self.skills))}")
        if self.summary:
            lines.append(f"Summary: {self.summary}")
        if self.companies:
            lines.append("Experience:")
            for entry in self.companies[:max_experiences]:
                span = f"{entry.get('start_date') or '?'} - {entry.get('end_date') or 'present'}"
                meta = ", ".join(
                    v for v in (entry.get("company_industry"), entry.get("company_stage"),
                                entry.get("company_size")) if v
                )
                head = f"  - {entry.get('title', '')} at {entry.get('company', '')} ({span})"
                lines.append(f"{head}{f' [{meta}]' if meta else ''}")
                description = (entry.get("description") or "").strip()
                if description:
                    lines.append(f"      {description[:280]}")
        if self.schools:
            # The three education lists are parallel but can be ragged (a row
            # may have a school and no degree), so index defensively.
            entries = []
            for position, school in enumerate(self.schools):
                degree = self.degrees[position] if position < len(self.degrees) else ""
                study = (
                    self.fields_of_study[position]
                    if position < len(self.fields_of_study)
                    else ""
                )
                entries.append(" ".join(part for part in (degree, study, school) if part))
            lines.append(f"Education: {'; '.join(entries)}")
        return "\n".join(lines)


def candidate_to_doc(candidate: Candidate) -> CandidateDoc:
    location_parts = [
        candidate.location_city,
        candidate.location_region,
        candidate.location_country,
    ]
    companies = [
        {
            "company": exp.company,
            "company_normalized": tx.normalize_company(exp.company),
            "company_domain": exp.company_domain,
            "company_industry": exp.company_industry,
            "company_size": exp.company_size,
            "company_stage": exp.company_stage,
            "title": exp.title,
            "title_normalized": exp.title_normalized,
            "seniority": exp.seniority,
            "start_date": exp.start_date,
            "end_date": exp.end_date,
            "is_current": exp.is_current,
            "description": exp.description,
        }
        for exp in candidate.experiences
    ]
    doc_text = candidate.doc_text or ""
    haystack = tx.slugify_text(
        " ".join(
            filter(
                None,
                [
                    doc_text,
                    candidate.headline,
                    candidate.summary,
                    " ".join(candidate.skills or []),
                ],
            )
        )
    )
    return CandidateDoc(
        id=candidate.id,
        full_name=candidate.full_name,
        headline=candidate.headline,
        summary=candidate.summary,
        current_title=candidate.current_title,
        current_title_normalized=candidate.current_title_normalized
        or tx.normalize_title(candidate.current_title),
        current_company=candidate.current_company,
        current_company_normalized=tx.normalize_company(candidate.current_company),
        seniority=candidate.seniority or "unknown",
        years_experience=float(candidate.years_experience or 0.0),
        skills=set(candidate.skills or []),
        industries={i for i in (candidate.industries or []) if i},
        location_parts=[p for p in location_parts if p],
        location_city=candidate.location_city or "",
        location_region=candidate.location_region or "",
        location_country=candidate.location_country or "",
        open_to_remote=bool(candidate.open_to_remote),
        emails=list(candidate.emails or []),
        linkedin_url=candidate.linkedin_url or "",
        doc_text=doc_text,
        haystack=haystack,
        titles_held=[c["title_normalized"] for c in companies if c["title_normalized"]],
        companies=companies,
        schools=[tx.slugify_text(e.school) for e in candidate.educations if e.school],
        degrees=[tx.slugify_text(e.degree) for e in candidate.educations if e.degree],
        fields_of_study=[
            tx.slugify_text(e.field_of_study) for e in candidate.educations if e.field_of_study
        ],
        updated_at=candidate.updated_at,
    )


class CorpusIndex:
    """Immutable snapshot: docs + BM25 index + embedding matrix, in row order."""

    def __init__(self, docs: list[CandidateDoc], embeddings: np.ndarray, fingerprint: str) -> None:
        self.docs = docs
        self.embeddings = embeddings
        self.fingerprint = fingerprint
        self.by_id = {doc.id: position for position, doc in enumerate(docs)}
        self.bm25 = BM25Index([tokenize(doc.doc_text) for doc in docs])

    def __len__(self) -> int:
        return len(self.docs)

    def stats(self) -> dict[str, Any]:
        return {
            "documents": len(self.docs),
            "vocabulary": self.bm25.vocabulary_size,
            "embedding_dim": int(self.embeddings.shape[1]) if self.embeddings.size else 0,
        }


_cache: dict[str, CorpusIndex] = {}
_lock = threading.Lock()


def _fingerprint(db: Session, org_id: str) -> str:
    row = db.execute(
        select(func.count(Candidate.id), func.max(Candidate.updated_at)).where(
            Candidate.org_id == org_id
        )
    ).one()
    count, latest = row[0] or 0, row[1]
    return f"{count}:{latest.isoformat() if latest else '-'}"


def _load_docs(db: Session, org_id: str) -> list[CandidateDoc]:
    candidates = (
        db.execute(
            select(Candidate)
            .where(Candidate.org_id == org_id)
            .options(
                selectinload(Candidate.experiences),
                selectinload(Candidate.educations),
            )
            .order_by(Candidate.id)
        )
        .scalars()
        .all()
    )
    return [candidate_to_doc(c) for c in candidates]


def _load_embeddings(db: Session, org_id: str, docs: list[CandidateDoc]) -> np.ndarray:
    embedder = get_embedder()
    rows = db.execute(
        select(Candidate.id, Candidate.embedding).where(Candidate.org_id == org_id)
    ).all()
    stored = {row[0]: row[1] for row in rows}

    dim = embedder.dim
    matrix = np.zeros((len(docs), dim), dtype=np.float32)
    missing: list[int] = []
    for position, doc in enumerate(docs):
        vector = stored.get(doc.id)
        if vector and len(vector) == dim:
            matrix[position] = np.asarray(vector, dtype=np.float32)
        else:
            missing.append(position)

    if missing:
        computed = embedder.embed([docs[i].doc_text for i in missing])
        for slot, position in enumerate(missing):
            matrix[position] = computed[slot]
    return matrix


def get_index(db: Session, org_id: str, *, force: bool = False) -> CorpusIndex:
    fingerprint = _fingerprint(db, org_id)
    with _lock:
        cached = _cache.get(org_id)
        if cached is not None and cached.fingerprint == fingerprint and not force:
            return cached

    docs = _load_docs(db, org_id)
    embeddings = _load_embeddings(db, org_id, docs)
    index = CorpusIndex(docs, embeddings, fingerprint)
    with _lock:
        _cache[org_id] = index
    return index


def invalidate(org_id: str | None = None) -> None:
    with _lock:
        if org_id is None:
            _cache.clear()
        else:
            _cache.pop(org_id, None)
