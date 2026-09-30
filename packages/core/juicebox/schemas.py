"""Pydantic request/response contracts for the HTTP API."""

from __future__ import annotations

from datetime import date, datetime
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


class OrgSendingOut(BaseModel):
    paused: bool
    paused_at: datetime | None
    paused_by_email: str | None
    held_count: int


class OrgSendingPatch(BaseModel):
    paused: bool


class InFlightRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enrollment_id: str
    candidate_id: str
    candidate_name: str
    candidate_email: str
    sequence_id: str
    sequence_name: str
    step: int
    mailbox: str
    due_at: datetime | None
    state: str
    reason: str | None = None
    # Only on state == "cancelled": when the operator stopped it.
    cancelled_at: datetime | None = None


class InFlightList(BaseModel):
    total: int
    rows: list[InFlightRow]
    # Stopped-from-the-outbox rows a re-enrol could bring back, whatever view
    # this page is showing -- so the live view can point at the cancelled one.
    cancelled_total: int = 0


class InFlightCancelIn(BaseModel):
    enrollment_ids: list[str] = Field(min_length=1, max_length=1000)
    action: Literal["skip", "stop"]


class InFlightCancelOut(BaseModel):
    skipped: int
    stopped: int
    already_sending: int
    not_found: int


class MailboxConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    address: str
    display_name: str
    status: str
    error: str
    user_id: str | None
    thread_sync_enabled: bool
    send_enabled: bool
    daily_send_cap: int
    last_synced_at: datetime | None
    created_at: datetime
    # Rolling 24h volume, computed per row by the list endpoint. Sent here so
    # the settings page can show "12 / 40" without a query per mailbox.
    sent_last_24h: int = 0


class MailboxProviderOut(BaseModel):
    name: str
    configured: bool
    redirect_uri: str


class MailboxIntegrationOut(BaseModel):
    providers: list[MailboxProviderOut]
    connections: list[MailboxConnectionOut]


class MailboxConnectOut(BaseModel):
    auth_url: str


class MailboxSendingPatch(BaseModel):
    """Rotation controls for one mailbox. Unset fields are left alone."""

    send_enabled: bool | None = None
    # 0 means "stop using this mailbox", not "unlimited".
    daily_send_cap: int | None = Field(default=None, ge=0, le=10_000)


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


class KnownContactIdentity(BaseModel):
    """One row of an import, before it is a record.

    ``key`` is the client's own row key and is echoed back untouched -- the
    server never sees the file, so it cannot say "row 41" and the caller must
    be able to line the answer up with its own table. Every identifying field
    is optional because a Juicebox export carries whichever columns the user
    ticked.
    """

    key: str = Field(min_length=1, max_length=200)
    emails: list[str] = Field(default_factory=list, max_length=10)
    linkedin_url: str = ""
    full_name: str = ""
    company: str = ""


class KnownContactsRequest(BaseModel):
    people: list[KnownContactIdentity] = Field(default_factory=list)


class KnownContactMatch(BaseModel):
    key: str
    candidate_id: str
    full_name: str
    # email | linkedin | name_and_company -- duplicates.MATCHED_ON, strongest
    # first. The client renders the reason; it must not re-derive it.
    matched_on: str
    reason: str


class KnownContactsResponse(BaseModel):
    matches: list[KnownContactMatch]
    # True when the LinkedIn pass hit its scan cap, so "no match" stops meaning
    # "definitely new". The page says so rather than implying a clean file.
    truncated: bool = False


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
    phones: list[str] = Field(default_factory=list)
    # CRM state. Without these the record page's status control cannot show
    # what is actually stored, and the UI has no way to mark which fields were
    # hand-edited.
    crm_status: str = "none"
    crm_tracked: bool = False
    manual_fields: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    experiences: list[ExperienceOut] = Field(default_factory=list)
    educations: list[EducationOut] = Field(default_factory=list)
    updated_at: datetime


class HistoryStatsOut(BaseModel):
    first_contacted_at: datetime | None = None
    last_emailed_at: datetime | None = None
    last_reply_at: datetime | None = None
    outbound_count: int = 0
    inbound_count: int = 0


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
    # Empty from the search path: hits are built from the in-memory index doc,
    # which carries addresses but not numbers. That is honest rather than a gap
    # -- no search surface shows a phone -- and it is why this has a default.
    phones: list[str] = Field(default_factory=list)
    crm_status: str = "none"
    crm_tracked: bool = False
    tags: list[str] = Field(default_factory=list)
    # The grid's "Added" column, and what its Recently-added view sorts on.
    # Optional because search hits are built from the in-memory index doc,
    # which carries no timestamps -- those never render in the grid.
    created_at: datetime | None = None
    # Voice-intake grid only. Optional so every other caller is unchanged.
    intake_timeline: str | None = None
    intake_target_comp: float | None = None
    intake_match_count: int | None = None
    intake_confirmed: bool | None = None
    # Populated only when the caller asked for history (include_history=true) —
    # None means "not requested", not "never contacted".
    history: HistoryStatsOut | None = None


# --------------------------------------------------------------------------
# crm
# --------------------------------------------------------------------------


class CandidatePatch(BaseModel):
    """A hand edit. Every field optional, and ``exclude_unset`` is what makes
    "clear this" (empty string) different from "leave it alone" (absent)."""

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    summary: str | None = None
    location_city: str | None = None
    location_region: str | None = None
    location_country: str | None = None
    open_to_remote: bool | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    website_url: str | None = None
    emails: list[str] | None = None
    phones: list[str] | None = None
    current_title: str | None = None
    current_company: str | None = None
    current_company_domain: str | None = None
    seniority: str | None = None
    crm_status: str | None = None
    tags: list[str] | None = None


class CandidateNoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


class CandidateNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    author_id: str | None = None
    author_name: str = ""
    body: str
    created_at: datetime
    edited_at: datetime | None = None


class DiscoveredOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    display_name: str = ""
    domain: str = ""
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    outbound_count: int = 0
    to_count: int = 0
    cc_count: int = 0
    thread_count: int = 0
    mailbox_ids: list[str] = Field(default_factory=list)
    status: str = "new"
    suggested_status: str = ""
    candidate_id: str | None = None


class DiscoveredPromoteRequest(BaseModel):
    ids: list[str]
    crm_status: str | None = None


class DiscoveredDismissRequest(BaseModel):
    ids: list[str]
    reason: str = ""


class IgnoreDomainRequest(BaseModel):
    domain: str


class MergeRequest(BaseModel):
    primary_id: str
    duplicate_ids: list[str]


class DismissDuplicateRequest(BaseModel):
    candidate_ids: list[str]


class DiscoveredBulkResult(BaseModel):
    promoted: int = 0
    dismissed: int = 0
    candidate_ids: list[str] = Field(default_factory=list)
    # email -> why it could not be promoted. Reported rather than swallowed:
    # upsert_candidates silently skips a payload it cannot normalise.
    failed: dict[str, str] = Field(default_factory=dict)


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
    # A pasted job description routinely runs past 4k characters; the JD
    # compiler truncates what it sends the model, so the cap here only needs
    # to stop an accidental file paste.
    query: str = Field(min_length=1, max_length=20000)
    project_id: str | None = None
    limit: int = Field(default=25, ge=1, le=200)
    overrides: SearchOverrides | None = None
    rerank: bool | None = None
    persist: bool = True
    # Off by default. Most of a database has never had an intake call, and
    # "never asked" is not "not interested" -- turning this on by default
    # would hide almost everybody.
    only_available: bool = False


class AvailabilityOut(BaseModel):
    """What the person last said about moving, as search reads it.

    ``state`` is one of ``availability.MARKET_STATES``. ``unknown`` means
    nobody has asked -- it is not a soft no, and the UI has to say so.
    """

    state: str = "unknown"
    label: str = "Not asked"
    detail: str = ""
    score: float = 0.0
    employment_status: str = ""
    timeline: str = ""
    days_since: int | None = None
    stale: bool = False
    target_base: float | None = None
    target_total: float | None = None
    comp_currency: str = ""
    active_processes: int = 0
    offers_in_hand: int = 0
    willing_to_relocate: bool | None = None
    work_authorization: str = ""
    reason_for_looking: str = ""
    crm_status: str = ""
    confirmed: bool = False


class InboxDraftOut(BaseModel):
    """One queued reply, as the review screen sees it."""

    id: str
    mailbox_id: str
    status: str
    from_address: str
    from_name: str = ""
    subject: str = ""
    inbound_body: str = ""
    received_at: datetime | None = None
    web_link: str = ""
    body: str = ""
    generated_by: str = ""
    # The model declined to write this one -- it wants a person. Distinct from
    # `drafting`, which only means the worker has not got to it yet.
    needs_a_human: bool = False
    drafting: bool = False
    edited: bool = False
    candidate_id: str | None = None
    candidate_name: str = ""
    draft_web_link: str = ""
    error: str = ""
    created_at: datetime | None = None


class InboxDraftListOut(BaseModel):
    drafts: list[InboxDraftOut] = Field(default_factory=list)
    pending_count: int = 0


class InboxDraftUpdate(BaseModel):
    # Optional, and None is not "": approving without a body means "send what
    # you drafted", and a plain `str` default made that indistinguishable from
    # "replace it with nothing" -- which silently cleared the draft.
    # Long enough for a real reply, bounded so the queue cannot be used to
    # stuff arbitrary text into somebody's mailbox.
    body: str | None = Field(default=None, max_length=20000)


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
    availability: AvailabilityOut = Field(default_factory=AvailabilityOut)
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
    company: str = ""


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    role_title: str
    description: str
    location: str
    company: str = ""
    status: str
    created_at: datetime


class AddToProjectRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1)
    stage: str = "sourced"
    search_id: str | None = None


class StageUpdate(BaseModel):
    stage: str
    notes: str | None = None
    # "contacted"/"replied" are set automatically by outreach; override records a
    # manual touch (e.g. the recruiter reached out themselves outside the system).
    override: bool = False


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
    track_opens: bool = True
    track_clicks: bool = True
    from_email: str = ""
    from_name: str = ""
    # Left unset, a payload carrying a mailbox_id is taken to mean "send from
    # that one" -- see _resolve_sender_mode in routers/outreach.py. Being
    # explicit here always wins.
    sender_mode: Literal["rotate", "fixed"] | None = None
    mailbox_id: str | None = None
    # Under "rotate", the mailboxes to rotate over. Empty means all of them.
    mailbox_ids: list[str] = Field(default_factory=list)


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
    track_opens: bool = True
    track_clicks: bool = True
    from_email: str
    from_name: str
    sender_mode: str
    mailbox_id: str | None
    mailbox_ids: list[str]
    steps: list[SequenceStepOut]


class CloneSequenceRequest(BaseModel):
    """Where the copy goes. Both fields optional; omitting both is the old behaviour.

    ``project_id`` exists because the copy used to inherit the source's project
    unconditionally, which put a sequence cloned from another project's template
    into *that* project while the recruiter was looking at their own -- and the
    Sequences list filters on project, so the copy simply was not there.

    ``None`` means "keep the source's", not "clear it": clearing would strand
    the copy outside every project view. Use PATCH to detach one deliberately.
    """

    project_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)


class SequenceUpdate(BaseModel):
    """Partial metadata edit — unset fields are left alone."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    project_id: str | None = None
    personalize: bool | None = None
    stop_on_reply: bool | None = None
    track_opens: bool | None = None
    track_clicks: bool | None = None
    from_email: str | None = None
    from_name: str | None = None
    sender_mode: Literal["rotate", "fixed"] | None = None
    # Explicit null clears the pin. Under sender_mode="rotate" the pool decides
    # anyway; with no mailbox connected at all, sends fall through to the
    # globally configured provider.
    mailbox_id: str | None = None
    # Narrow which mailboxes "rotate" may use. An empty list widens it back to
    # every eligible mailbox, so it clears the choice rather than meaning
    # "none" -- a sequence that can never send is not a state worth being able
    # to save.
    mailbox_ids: list[str] | None = None


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
    # True = ignore the first step's send_at_time for this enrolment and make
    # it due immediately. Distinct from start_immediately, which controls
    # whether a next_send_at is set at all.
    send_now: bool = False
    # Enrol people who replied to us within the quiet period anyway. The
    # default skips them and reports who in EnrollResponse.skipped.
    force: bool = False


class EnrollmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sequence_id: str
    candidate_id: str
    status: str
    current_step: int
    next_send_at: datetime | None
    stopped_reason: str
    replied_at: datetime | None = None
    follow_up_after: date | None = None
    follow_up_reminder_sent_at: datetime | None = None
    snoozed_until: datetime | None = None
    candidate: CandidateSummary


class EnrollSkipOut(BaseModel):
    candidate_id: str
    full_name: str
    reason: Literal["replied_recently"]
    replied_at: datetime | None
    reply_intent: str
    quiet_days: int


class EnrollResponse(BaseModel):
    enrollments: list[EnrollmentOut]
    skipped: list[EnrollSkipOut]


class InFlightReenrollIn(BaseModel):
    enrollment_ids: list[str] = Field(min_length=1, max_length=1000)
    # now: due immediately, and sent in this request when the sequence is
    # live. next_slot: the step's own send hour, next time it comes round,
    # with no delay counted. at: ``send_at``, as given.
    when: Literal["now", "next_slot", "at"] = "next_slot"
    send_at: datetime | None = None
    # Re-enrol people who replied to us by hand within the quiet period anyway.
    # The default reports them in ``skipped`` and leaves them stopped.
    force: bool = False


class InFlightReenrollOut(BaseModel):
    resumed: int
    completed: int
    sent: int
    not_found: int
    skipped: list[EnrollSkipOut]
    # The earliest new send time among the resumed rows, for the page's note.
    next_send_at: datetime | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    enrollment_id: str | None
    channel: str
    direction: str
    source: str
    from_address: str
    to_address: str
    subject: str
    body: str
    status: str
    conversation_id: str
    web_link: str
    attachment_count: int
    created_at: datetime
    sent_at: datetime | None
    opened_at: datetime | None
    clicked_at: datetime | None
    replied_at: datetime | None
    reply_intent: str


class ThreadOut(BaseModel):
    conversation_id: str
    subject: str
    last_at: datetime | None
    messages: list[MessageOut]


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: str
    subject_type: str
    subject_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class IntakeTimelineMessageOut(BaseModel):
    id: str
    direction: str
    body: str
    asked_field: str = ""
    status: str = ""
    created_at: datetime
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    read_at: datetime | None = None


class IntakeTimelineThreadOut(BaseModel):
    thread_id: str
    channel: str
    status: str
    last_at: datetime | None = None
    messages: list[IntakeTimelineMessageOut] = Field(default_factory=list)


class CandidateTimelineOut(BaseModel):
    stats: HistoryStatsOut
    threads: list[ThreadOut]
    events: list[EventOut] = Field(default_factory=list)
    intake_threads: list[IntakeTimelineThreadOut] = Field(default_factory=list)
    # Read live from OpenInstinct; JuiceBox does not copy its candidate chat
    # database into the recruiter store.
    openinstinct_conversations: list[dict[str, Any]] = Field(default_factory=list)
    openinstinct_available: bool = False
    openinstinct_application_tasks: list[dict[str, Any]] = Field(default_factory=list)


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
    # The inbound mail's own RFC Message-ID, when the ESP's parse payload
    # carries headers. It is the reply's identity: without one, a redelivered
    # webhook is only recognisable by hashing the delivery itself.
    message_id: str = Field(default="", max_length=300)

    model_config = ConfigDict(populate_by_name=True)


class PreviewRequest(BaseModel):
    candidate_id: str
    step_order: int = 0
    # A later step's {{ai: ...}} follow-up reads the thread; previewing against
    # a real enrollment shows it with that thread instead of reading like step 1.
    enrollment_id: str | None = None


class PreviewResponse(BaseModel):
    subject: str
    body: str
    personalization: dict[str, Any]
    # Which mailbox this preview was rendered as. Empty means the globally
    # configured provider, i.e. no mailbox is connected. Surfaced because a
    # preview signed by the wrong person is otherwise indistinguishable from a
    # correct one.
    sender_email: str = ""
    sender_name: str = ""
    # off: no directives in the step · resolved: model wrote them ·
    # unavailable: directives present but no model — body shows [AI: ...] markers.
    ai_status: Literal["off", "resolved", "unavailable"] = "off"
    # Placeholders the renderer cannot parse, verbatim (e.g. "{{First name}}").
    # The body above shows them as the candidate would see them — as raw braces
    # — so the warning is what tells the recruiter that is a bug, not a choice.
    # Non-empty means this step will refuse to send until it is fixed.
    warnings: list[str] = Field(default_factory=list)
    # Spam-risk of this rendered email (outreach/spamcheck.py).
    spam: SpamReportOut | None = None


class SpamFindingOut(BaseModel):
    code: str
    # subject | body | delivery. Delivery findings come from how every email
    # is sent (tracking), so the copy cannot fix them.
    area: str
    points: int
    title: str
    detail: str
    fix: str = ""
    matches: list[str] = Field(default_factory=list)


class SpamReportOut(BaseModel):
    score: int
    level: Literal["low", "medium", "high"]
    content_score: int
    delivery_score: int
    word_count: int
    findings: list[SpamFindingOut] = Field(default_factory=list)


class SpamCheckStep(BaseModel):
    subject_template: str = Field(default="", max_length=2000)
    body_template: str = Field(default="", max_length=100_000)


class SpamCheckRequest(BaseModel):
    # In step order. Step 1's subject is the thread's; later steps are
    # replies and their own subject is ignored.
    steps: list[SpamCheckStep] = Field(default_factory=list, max_length=20)
    # The sequence's tracking switches, so the delivery findings match what
    # will actually be sent.
    track_opens: bool = True
    track_clicks: bool = True


class SpamCheckResponse(BaseModel):
    results: list[SpamReportOut]


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


# --------------------------------------------------------------------------
# job postings / matches
# --------------------------------------------------------------------------


class JobPostingIn(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    company: str = ""
    location: str = ""
    jd_text: str = ""
    source_url: str = ""
    seniority: str = ""
    open_to_remote: bool = False
    project_id: str | None = None


class JobPostingPatch(BaseModel):
    title: str | None = None
    company: str | None = None
    location: str | None = None
    jd_text: str | None = None
    source_url: str | None = None
    seniority: str | None = None
    open_to_remote: bool | None = None
    status: str | None = None
    project_id: str | None = None


class JobPostingImport(BaseModel):
    postings: list[JobPostingIn] = Field(min_length=1, max_length=500)
    source: str = "csv"


class JobPostingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    source: str
    source_id: str
    source_url: str
    company: str
    company_normalized: str
    title: str
    title_normalized: str
    location: str
    open_to_remote: bool
    seniority: str
    jd_text: str
    status: str
    parsed_spec: dict[str, Any] = Field(default_factory=dict)
    spec_version: str
    parser: str
    parsed_at: datetime | None
    embedding_model: str
    fee_bearing: bool
    fee_terms: str
    fee_evidence: str
    referral_paths: list[Any] = Field(default_factory=list)
    has_warm_path: bool
    match_count: int
    project_id: str | None
    posted_at: datetime | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    created_at: datetime


class JobMatchPatch(BaseModel):
    status: Literal["new", "shortlisted", "dismissed"]
    dismissed_reason: str = ""


class JobMatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    job_posting_id: str
    candidate_id: str
    score: float
    heuristic_score: float
    vector_score: float
    criteria: list[Any] = Field(default_factory=list)
    status: str
    dismissed_reason: str
    spec_version: str
    computed_at: datetime | None
    candidate: CandidateSummary | None = None
    posting: JobPostingOut | None = None


class RematchQueueRow(BaseModel):
    candidate: CandidateSummary
    reply_intent: str
    declined_project_id: str | None
    declined_label: str
    declined_on: datetime | None
    matches: list[JobMatchOut]


class CreateProjectFromPosting(BaseModel):
    name: str | None = None


class DiscoverMessage(BaseModel):
    role: Literal["user", "agent"]
    content: str = ""


class DiscoverStep(BaseModel):
    label: str
    detail: str = ""
    count: int = 0


class DiscoveredPosting(BaseModel):
    source_id: str
    title: str
    company: str = ""
    location: str = ""
    jd_text: str = ""
    source_url: str = ""
    already_have: bool = False
    posting_id: str | None = None


class DiscoverTurnIn(BaseModel):
    messages: list[DiscoverMessage] = Field(min_length=1)
    num_results: int = Field(default=10, ge=1, le=25)


class DiscoverTurnOut(BaseModel):
    reply: str
    questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    steps: list[DiscoverStep] = Field(default_factory=list)
    results: list[DiscoveredPosting] = Field(default_factory=list)


class IntakeFieldAnswerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    intake_call_id: str
    candidate_id: str | None
    field_key: str
    value: Any = None
    verbatim: str
    confidence: str
    declined: bool
    asked: bool


class CandidatePreferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    source_call_id: str | None
    source_thread_id: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    schema_version: str
    employment_status: str
    reason_for_looking: str
    timeline: str
    urgency_driver: str
    target_base: float | None
    target_total: float | None
    comp_currency: str
    work_authorization: str
    willing_to_relocate: bool | None
    active_processes: list[Any] = Field(default_factory=list)
    offers_in_hand: list[Any] = Field(default_factory=list)
    highlights: list[Any] = Field(default_factory=list)
    # Recruiter-only. Must never reach a client-facing surface.
    confidential_items: list[Any] = Field(default_factory=list)
    ok_to_share_with_clients: bool | None
    declined: list[Any] = Field(default_factory=list)
    low_confidence: list[Any] = Field(default_factory=list)
    fields_missing: list[Any] = Field(default_factory=list)
    review_needed: bool
    review_reason: str
    confirmed_at: datetime | None
    confirmed_by: str
    captured_at: datetime | None


class IntakeCallSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str | None
    entry_point: str
    from_number: str
    to_number: str
    # What the caller said they were called and where to reach them. On the
    # review queue this is the difference between "unknown caller" and a row a
    # recruiter can attribute in one click.
    contact_name: str = ""
    contact_email: str = ""
    status: str
    duration_seconds: int
    required_complete: bool
    review_needed: bool
    review_reason: str
    gate_mode: str
    minutes_available: int | None
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime


class IntakeCallOut(IntakeCallSummary):
    transcript: str
    recording_url: str
    extracted: dict[str, Any] = Field(default_factory=dict)
    deferred_fields: list[Any] = Field(default_factory=list)
    schema_version: str
    preference: CandidatePreferenceOut | None = None
    field_answers: list[IntakeFieldAnswerOut] = Field(default_factory=list)


class IntakeThreadSummary(BaseModel):
    """A messaging thread on the review queue, beside the calls.

    Deliberately a sibling shape rather than a widened ``IntakeCallSummary``:
    a thread has no duration or recording, and a call has no message counts.
    Unifying them is the ``IntakeSession`` refactor, which is a rename across
    two tables and not something a review queue should force.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str | None
    channel: str
    entry_point: str
    status: str
    contact_name: str
    contact_email: str
    contact_linkedin: str = ""
    from_address: str
    required_complete: bool
    review_needed: bool
    review_reason: str
    inbound_count: int
    outbound_count: int
    last_inbound_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class IntakeMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    direction: str
    body: str
    asked_field: str = ""
    status: str = ""
    seq: int = 0
    created_at: datetime
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    read_at: datetime | None = None


class IntakeThreadOut(IntakeThreadSummary):
    """Full thread for the recruiter detail / candidate Intake tab."""

    messages: list[IntakeMessageOut] = Field(default_factory=list)
    record: dict[str, Any] = Field(default_factory=dict)
    fields_missing: list[Any] = Field(default_factory=list)


class IntakeThreadPatch(BaseModel):
    candidate_id: str


class IntakeThreadCountOut(BaseModel):
    count: int


class IntakeCallPatch(BaseModel):
    candidate_id: str


class IntakeCallCountOut(BaseModel):
    count: int


TokenResponse.model_rebuild()


# --------------------------------------------------------------------------
# applications and candidate documents
# --------------------------------------------------------------------------


class ApplicationCreate(BaseModel):
    candidate_id: str
    job_posting_id: str
    source: str = "recruiter"


class ApplicationTransitionIn(BaseModel):
    to_status: str
    reason: str = ""
    # When the thing actually happened, which is rarely when somebody got
    # round to typing it in. Nothing in the state machine reads it -- it is
    # recorded on the event so the history reads as the employer's timeline
    # rather than ours. Absent means "now".
    occurred_at: datetime | None = None


class ApplicationPatch(BaseModel):
    submission_url: str | None = None
    status_reason: str | None = None
    form_answers: list[dict[str, Any]] | None = None


class CandidateDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    candidate_id: str
    application_id: str | None
    kind: str
    is_default: bool
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    schema_version: str
    generated_by: str
    source: str
    error: str
    created_at: datetime
    updated_at: datetime


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    candidate_id: str
    job_posting_id: str
    job_match_id: str | None
    proposal_id: str | None
    project_id: str | None
    source: str
    status: str
    status_reason: str
    consenting_message_id: str | None
    approved_at: datetime | None
    consent_verbatim: str
    submission_url: str
    submission_method: str
    submitted_by: str
    form_answers: list[Any] = Field(default_factory=list)
    networking: list[Any] = Field(default_factory=list)
    package_error: str
    prepared_at: datetime | None
    submitted_at: datetime | None
    acknowledged_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ApplicationHistoryEntry(BaseModel):
    """One recorded move, replayed from the event log.

    Read out of ``events`` rather than a table of its own: every transition
    already writes ``application.status_changed`` with exactly this content,
    and a second copy would be one more thing that can disagree with the row.
    """

    at: datetime
    occurred_at: datetime | None = None
    from_status: str = ""
    to_status: str = ""
    actor: str = ""
    note: str = ""


class ApplicationDetailOut(ApplicationOut):
    """List row plus joined display fields, current documents and history."""

    company: str = ""
    title: str = ""
    candidate_name: str = ""
    # The posting's own URL, which is not `submission_url`: that one is where a
    # human applies and is often blank. This is the JD, and it is what somebody
    # scanning the list actually wants to open.
    posting_url: str = ""
    documents: list[CandidateDocumentOut] = Field(default_factory=list)
    history: list[ApplicationHistoryEntry] = Field(default_factory=list)


# --------------------------------------------------------------------------
# client dashboards
# --------------------------------------------------------------------------
#
# Two response shapes for one row on purpose. ClientSubmissionOut is what a
# client session is served and carries no CRM identifier; the recruiter shape
# extends it. A field added to the recruiter view cannot leak to a client
# because the client serialiser does not inherit from it.


class ClientLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class ClientUserOut(BaseModel):
    id: str
    username: str
    name: str
    client_account_id: str
    client_name: str
    kind: Literal["client"] = "client"


class ClientTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: ClientUserOut


class ClientNoteOut(BaseModel):
    id: str
    author_kind: str
    author_name: str
    body: str
    created_at: datetime
    # Whether the caller wrote it, so the UI offers delete on the right notes
    # without being handed anybody's user id.
    mine: bool = False


class ClientEventOut(BaseModel):
    from_stage: str
    to_stage: str
    actor_kind: str
    actor_name: str
    created_at: datetime


class ClientSubmissionOut(BaseModel):
    id: str
    full_name: str
    current_title: str
    current_company: str
    location: str
    linkedin_url: str
    role_title: str
    stage: str
    stage_changed_at: datetime
    stage_changed_by_kind: str
    stage_changed_by_name: str
    shared_at: datetime
    note_count: int = 0
    latest_note: ClientNoteOut | None = None


class ClientSubmissionDetail(ClientSubmissionOut):
    notes: list[ClientNoteOut] = []
    events: list[ClientEventOut] = []


class RecruiterSubmissionOut(ClientSubmissionOut):
    candidate_id: str | None = None
    # Fields edited on the dashboard rather than read from the CRM contact.
    overridden_fields: list[str] = []


class RecruiterSubmissionDetail(RecruiterSubmissionOut):
    notes: list[ClientNoteOut] = []
    events: list[ClientEventOut] = []


class ClientStageIn(BaseModel):
    stage: str


class ClientNoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class ClientAccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ClientAccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class ClientLoginIn(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)
    name: str = ""


class ClientLoginPatch(BaseModel):
    password: str | None = Field(default=None, min_length=8, max_length=200)
    name: str | None = None
    is_active: bool | None = None


class ClientLoginOut(BaseModel):
    id: str
    username: str
    name: str
    is_active: bool
    last_login_at: datetime | None = None


class ClientAccountOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    logins: list[ClientLoginOut] = []
    stage_counts: dict[str, int] = {}
    total: int = 0
    # Stage moves and notes made by the client in the last seven days: the
    # number that says "go and look" on the recruiter's list of clients.
    client_activity_7d: int = 0


class ClientEntryIn(BaseModel):
    candidate_id: str | None = None
    full_name: str = Field(default="", max_length=200)
    linkedin_url: str = Field(default="", max_length=400)
    current_title: str = Field(default="", max_length=240)
    current_company: str = Field(default="", max_length=240)
    role_title: str = Field(default="", max_length=200)
    stage: str = ""


class ClientEntriesIn(BaseModel):
    entries: list[ClientEntryIn] = Field(default_factory=list, max_length=2000)
    # Shares every member of one of our projects, e.g. the pipeline we have
    # been sending this client over LinkedIn.
    project_id: str | None = None
    # Applied to entries that do not carry their own.
    role_title: str = Field(default="", max_length=200)
    stage: str = ""


class ClientEntryResult(BaseModel):
    line: int
    outcome: Literal["added", "updated", "unchanged", "skipped"]
    submission_id: str | None = None
    full_name: str = ""
    matched_contact: bool = False
    reason: str = ""


class ClientEntriesOut(BaseModel):
    added: int
    updated: int
    unchanged: int
    skipped: int
    results: list[ClientEntryResult]


class RecruiterSubmissionPatch(BaseModel):
    stage: str | None = None
    role_title: str | None = Field(default=None, max_length=200)
    full_name: str | None = Field(default=None, max_length=200)
    current_title: str | None = Field(default=None, max_length=240)
    current_company: str | None = Field(default=None, max_length=240)
    location: str | None = Field(default=None, max_length=240)
    linkedin_url: str | None = Field(default=None, max_length=400)


class ClientBulkStageIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=2000)
    stage: str
