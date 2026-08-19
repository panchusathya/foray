"""Pydantic request/response contracts for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


class SignupRequest(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: str = ""


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    email: str
    name: str
    role: str


class ApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    key: str | None = None
    created_at: datetime


class OrgOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str


class OrgPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class MailboxConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    address: str
    display_name: str
    status: str
    error: str
    user_id: str | None
    last_synced_at: datetime | None
    created_at: datetime


class MicrosoftIntegrationOut(BaseModel):
    configured: bool
    redirect_uri: str
    connections: list[MailboxConnectionOut]


class MicrosoftConnectOut(BaseModel):
    auth_url: str


class ReplyDraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    mailbox_id: str
    inbound_message_id: str
    candidate_id: str | None
    web_link: str
    body: str
    generated_by: str
    intent: str
    status: str
    created_at: datetime


class SuppressionIn(BaseModel):
    email: EmailStr
    reason: Literal["unsubscribe", "bounce", "manual"] = "manual"


class SuppressionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    reason: str
    source: str
    created_at: datetime


# --------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------


class ExperienceIn(BaseModel):
    company: str = ""
    company_domain: str = ""
    company_industry: str = ""
    company_size: str = ""
    company_stage: str = ""
    title: str = ""
    start_date: str = ""
    end_date: str = ""
    is_current: bool = False
    location: str = ""
    description: str = ""


class EducationIn(BaseModel):
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    start_year: int | None = None
    end_year: int | None = None


class CandidateIn(BaseModel):
    source: str = "manual"
    source_id: str | None = None
    full_name: str
    headline: str = ""
    summary: str = ""
    location: str = ""
    location_city: str = ""
    location_region: str = ""
    location_country: str = ""
    open_to_remote: bool = False
    linkedin_url: str = ""
    github_url: str = ""
    website_url: str = ""
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    # normalize_candidate() reads these, but pydantic's extra='ignore' plus
    # model_dump(exclude_none=True) dropped them before they reached it, so an
    # importer that has no experiences[] (e.g. Foray) lost employer and
    # experience. years_experience stays None-default, NOT 0.0: normalize only
    # computes from experiences when it is None, so a 0.0 default would zero out
    # every import that relies on the computed value.
    current_title: str = ""
    current_company: str = ""
    current_company_domain: str = ""
    seniority: str = ""
    years_experience: float | None = None
    experiences: list[ExperienceIn] = Field(default_factory=list)
    educations: list[EducationIn] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class BulkCandidateIn(BaseModel):
    candidates: list[CandidateIn]


class BulkUpsertResult(BaseModel):
    created: int
    updated: int
    ids: list[str]


class ExperienceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    company: str
    company_domain: str
    company_industry: str
    company_size: str
    company_stage: str
    title: str
    title_normalized: str
    seniority: str
    start_date: str
    end_date: str
    is_current: bool
    months: int
    location: str
    description: str


class EducationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    school: str
    degree: str
    field_of_study: str
    start_year: int | None
    end_year: int | None


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    source_id: str
    full_name: str
    first_name: str
    last_name: str
    headline: str
    summary: str
    location_city: str
    location_region: str
    location_country: str
    open_to_remote: bool
    linkedin_url: str
    github_url: str
    website_url: str
    emails: list[str]
    skills: list[str]
    industries: list[str]
    current_title: str
    current_company: str
    current_company_domain: str
    seniority: str
    years_experience: float
    experiences: list[ExperienceOut] = Field(default_factory=list)
    educations: list[EducationOut] = Field(default_factory=list)
    updated_at: datetime


class CandidateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    full_name: str
    headline: str
    current_title: str
    current_company: str
    location_city: str
    location_country: str
    seniority: str
    years_experience: float
    skills: list[str]
    linkedin_url: str
    emails: list[str]


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


class SearchOverrides(BaseModel):
    """Facet filters the UI can apply on top of (or instead of) the parsed query."""

    job_titles: list[str] | None = None
    must_have_skills: list[str] | None = None
    nice_to_have_skills: list[str] | None = None
    seniority: list[str] | None = None
    min_years_experience: float | None = None
    max_years_experience: float | None = None
    locations: list[str] | None = None
    remote_ok: bool | None = None
    industries: list[str] | None = None
    company_stages: list[str] | None = None
    company_sizes: list[str] | None = None
    target_companies: list[str] | None = None
    excluded_companies: list[str] | None = None
    keywords: list[str] | None = None
    negative_keywords: list[str] | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    project_id: str | None = None
    limit: int = Field(default=25, ge=1, le=200)
    overrides: SearchOverrides | None = None
    rerank: bool | None = None
    persist: bool = True


class CriterionOut(BaseModel):
    name: str
    label: str
    kind: str
    weight: float
    status: Literal["strong", "partial", "miss", "unknown"]
    score: float
    evidence: str
    sources: list[str] = Field(default_factory=list)


class SearchHit(BaseModel):
    rank: int
    candidate: CandidateSummary
    final_score: float
    heuristic_score: float
    fusion_score: float
    lexical_score: float
    vector_score: float
    rerank_score: float | None = None
    summary: str = ""
    concerns: list[str] = Field(default_factory=list)
    criteria: list[CriterionOut] = Field(default_factory=list)


class ParsedQueryOut(BaseModel):
    job_titles: list[str] = Field(default_factory=list)
    title_exclusions: list[str] = Field(default_factory=list)
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    min_years_experience: float | None = None
    max_years_experience: float | None = None
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool | None = None
    industries: list[str] = Field(default_factory=list)
    company_stages: list[str] = Field(default_factory=list)
    company_sizes: list[str] = Field(default_factory=list)
    target_companies: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    schools: list[str] = Field(default_factory=list)
    degrees: list[str] = Field(default_factory=list)
    fields_of_study: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    freeform_criteria: list[str] = Field(default_factory=list)
    search_text: str = ""


class SearchResponse(BaseModel):
    search_id: str | None
    query: str
    parser: str
    reranked: bool
    parsed: ParsedQueryOut
    total_matched: int
    latency_ms: int
    results: list[SearchHit]


# --------------------------------------------------------------------------
# projects / pipeline
# --------------------------------------------------------------------------


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role_title: str = ""
    description: str = ""
    location: str = ""


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    role_title: str
    description: str
    location: str
    status: str
    created_at: datetime


class AddToProjectRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1)
    stage: str = "sourced"
    search_id: str | None = None


class StageUpdate(BaseModel):
    stage: str
    notes: str | None = None


class PipelineEntryOut(BaseModel):
    id: str
    stage: str
    score: float
    notes: str
    rationale: dict[str, Any]
    added_at: datetime
    candidate: CandidateSummary


# --------------------------------------------------------------------------
# outreach
# --------------------------------------------------------------------------


class SequenceStepIn(BaseModel):
    channel: str = "email"
    delay_days: int = Field(default=0, ge=0, le=90)
    delay_unit: Literal["days", "business_days"] = "days"
    # "HH:MM" 24h clock; empty means "send whenever the step is due".
    send_at_time: str = Field(default="", pattern=r"^$|^([01][0-9]|2[0-3]):[0-5][0-9]$")
    send_timezone: str = Field(default="", max_length=64)
    subject_template: str = ""
    body_template: str


class SequenceIn(BaseModel):
    name: str
    project_id: str | None = None
    steps: list[SequenceStepIn] = Field(min_length=1)
    personalize: bool = True
    stop_on_reply: bool = True
    from_email: str = ""
    from_name: str = ""
    mailbox_id: str | None = None


class SequenceStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    step_order: int
    channel: str
    delay_days: int
    delay_unit: str
    send_at_time: str
    send_timezone: str
    subject_template: str
    body_template: str


class SequenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    project_id: str | None
    status: str
    personalize: bool
    stop_on_reply: bool
    from_email: str
    from_name: str
    mailbox_id: str | None
    steps: list[SequenceStepOut]


class SequenceUpdate(BaseModel):
    """Partial metadata edit — unset fields are left alone."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    project_id: str | None = None
    personalize: bool | None = None
    stop_on_reply: bool | None = None
    from_email: str | None = None
    from_name: str | None = None
    # Explicit null reverts to the globally configured provider.
    mailbox_id: str | None = None


class GenerateSequenceRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    project_id: str | None = None
    num_steps: int = Field(default=3, ge=1, le=4)


class GenerateSequenceResponse(BaseModel):
    """A draft only — nothing is persisted until the client POSTs it."""

    draft: SequenceIn
    generated_by: Literal["llm", "template"]


class EnrollRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1)
    project_id: str | None = None
    start_immediately: bool = True


class EnrollmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sequence_id: str
    candidate_id: str
    status: str
    current_step: int
    next_send_at: datetime | None
    stopped_reason: str


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    channel: str
    direction: str
    to_address: str
    subject: str
    body: str
    status: str
    sent_at: datetime | None
    opened_at: datetime | None
    clicked_at: datetime | None
    replied_at: datetime | None
    reply_intent: str


class RunSequenceResponse(BaseModel):
    processed: int
    sent: int
    failed: int
    completed: int
    skipped: int = 0
    messages: list[MessageOut]


class InboundEmail(BaseModel):
    to: str = ""
    from_address: str = Field(default="", alias="from")
    subject: str = ""
    body: str = ""
    in_reply_to: str | None = None
    candidate_email: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class PreviewRequest(BaseModel):
    candidate_id: str
    step_order: int = 0


class PreviewResponse(BaseModel):
    subject: str
    body: str
    personalization: dict[str, Any]


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------


class SourcingAgentIn(BaseModel):
    name: str
    project_id: str
    query: str
    overrides: SearchOverrides | None = None
    min_score: float = 0.6
    max_per_run: int = 10
    cadence_minutes: int = 1440
    auto_enroll_sequence_id: str | None = None


class SourcingAgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    project_id: str
    query: str
    min_score: float
    max_per_run: int
    cadence_minutes: int
    status: str
    auto_enroll_sequence_id: str | None
    last_run_at: datetime | None
    total_added: int


class AgentRunResult(BaseModel):
    agent_id: str
    evaluated: int
    added: int
    enrolled: int
    candidates: list[CandidateSummary]


# --------------------------------------------------------------------------
# analytics
# --------------------------------------------------------------------------


class FunnelStats(BaseModel):
    project_id: str | None = None
    stages: dict[str, int]
    outreach: dict[str, int]
    reply_rate: float
    open_rate: float
    positive_reply_rate: float


TokenResponse.model_rebuild()
