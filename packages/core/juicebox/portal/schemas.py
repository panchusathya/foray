"""Candidate-safe serializers — whitelists, never the recruiter schemas.

``CandidateOut`` leaks crm_status/manual_fields/source; ``JobMatchOut`` leaks
scores and criteria; ``CandidatePreferenceOut`` leaks confidential_items and
review flags. None of those types appear here, and the leak tests in
``test_portal_api.py`` assert the forbidden keys never cross this boundary —
that test list is the contract every future field addition answers to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..models import Application, Candidate, CandidateDocument, CandidatePreference, JobPosting

# Keys that must never appear anywhere in a portal response, at any depth.
FORBIDDEN_KEYS = frozenset(
    {
        "crm_status",
        "crm_tracked",
        "manual_fields",
        "confidential_items",
        "ok_to_share_with_clients",
        "score",
        "heuristic_score",
        "vector_score",
        "criteria",
        "recruiter_only",
        "review_needed",
        "review_reason",
        "low_confidence",
        "notes",
        "fee_bearing",
        "fee_terms",
        "fee_evidence",
    }
)


class PortalProfileOut(BaseModel):
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    headline: str = ""
    location_city: str = ""
    location_region: str = ""
    location_country: str = ""
    open_to_remote: bool = False
    linkedin_url: str = ""
    github_url: str = ""
    website_url: str = ""
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    current_title: str = ""
    current_company: str = ""
    skills: list[str] = Field(default_factory=list)

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> PortalProfileOut:
        return cls(
            first_name=candidate.first_name or "",
            last_name=candidate.last_name or "",
            full_name=candidate.full_name or "",
            headline=candidate.headline or "",
            location_city=candidate.location_city or "",
            location_region=candidate.location_region or "",
            location_country=candidate.location_country or "",
            open_to_remote=bool(candidate.open_to_remote),
            linkedin_url=candidate.linkedin_url or "",
            github_url=candidate.github_url or "",
            website_url=candidate.website_url or "",
            emails=[e for e in (candidate.emails or []) if isinstance(e, str)],
            phones=[p for p in (candidate.phones or []) if isinstance(p, str)],
            current_title=candidate.current_title or "",
            current_company=candidate.current_company or "",
            skills=[s for s in (candidate.skills or []) if isinstance(s, str)],
        )


class PortalProfilePatch(BaseModel):
    """What a candidate may edit about themselves. A strict subset of the
    recruiter's EDITABLE_FIELDS — nothing here touches employment history."""

    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    headline: str | None = None
    location_city: str | None = None
    location_region: str | None = None
    location_country: str | None = None
    open_to_remote: bool | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    website_url: str | None = None
    emails: list[str] | None = None
    phones: list[str] | None = None


class PortalPreferenceOut(BaseModel):
    timeline: str = ""
    employment_status: str = ""
    target_base: float | None = None
    target_total: float | None = None
    comp_currency: str = ""
    work_authorization: str = ""
    willing_to_relocate: bool | None = None
    highlights: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool | None = None

    @classmethod
    def from_preference(cls, preference: CandidatePreference) -> PortalPreferenceOut:
        spec = preference.spec or {}
        return cls(
            timeline=preference.timeline or "",
            employment_status=preference.employment_status or "",
            target_base=preference.target_base,
            target_total=preference.target_total,
            comp_currency=preference.comp_currency or "",
            work_authorization=preference.work_authorization or "",
            willing_to_relocate=preference.willing_to_relocate,
            highlights=[str(h) for h in (preference.highlights or [])],
            titles=[str(t) for t in (spec.get("job_titles") or [])],
            locations=[str(v) for v in (spec.get("locations") or [])],
            remote_ok=spec.get("remote_ok"),
        )


def portal_card(card: dict[str, Any]) -> dict[str, Any]:
    """The candidate's view of a jobcard projection: everything except the
    recruiter-only block. Reasons, never a score."""
    return {key: value for key, value in card.items() if key != "recruiter_only"}


class PortalDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    application_id: str | None
    kind: str
    is_default: bool
    filename: str
    content_type: str
    size_bytes: int
    status: str
    generated_by: str
    created_at: datetime
    updated_at: datetime


class PortalApplicationOut(BaseModel):
    id: str
    status: str
    company: str = ""
    title: str = ""
    location: str = ""
    submission_url: str = ""
    approved_at: datetime | None = None
    prepared_at: datetime | None = None
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    documents: list[PortalDocumentOut] = Field(default_factory=list)
    # The candidate's own answers, with provenance -- they wrote none of it,
    # so they get to see all of it before anything is sent.
    form_answers: list[dict[str, Any]] = Field(default_factory=list)
    # Whether we can actually send this one ourselves. False for most postings
    # (no adapter for that ATS) and for a candidate's first application, which
    # a person reviews. The portal hides the button rather than offering one
    # that answers 409 -- a button that cannot work is worse than no button.
    can_auto_submit: bool = False

    @classmethod
    def from_application(
        cls,
        application: Application,
        posting: JobPosting | None,
        documents: list[CandidateDocument] | None = None,
        can_auto_submit: bool = False,
    ) -> PortalApplicationOut:
        return cls(
            id=application.id,
            status=application.status,
            company=posting.company if posting else "",
            title=posting.title if posting else "",
            location=posting.location if posting else "",
            submission_url=application.submission_url or "",
            approved_at=application.approved_at,
            prepared_at=application.prepared_at,
            submitted_at=application.submitted_at,
            created_at=application.created_at,
            updated_at=application.updated_at,
            documents=[PortalDocumentOut.model_validate(doc) for doc in documents or []],
            form_answers=list(application.form_answers or []),
            can_auto_submit=can_auto_submit,
        )
