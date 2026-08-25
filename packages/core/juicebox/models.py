"""SQLAlchemy models — the whole recruiting domain.

Identifiers are prefixed ULID-ish strings (``cand_9f3a...``) so objects are
self-describing in logs and URLs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    false,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .types import EmbeddingVector


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------
# tenancy
# --------------------------------------------------------------------------


class Org(Base, TimestampMixin):
    __tablename__ = "orgs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("org"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)

    # Corporate domains that count as "us" during a discovery sweep, on top
    # of the ones derived from connected mailboxes. Editable because a firm
    # with several domains would otherwise re-stage its own staff forever.
    crm_internal_domains: Mapped[list] = mapped_column(JSON, default=list)

    users: Mapped[list[User]] = relationship(back_populates="org", cascade="all, delete-orphan")


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_user_org_email"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("usr"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="recruiter")  # owner|recruiter|viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    org: Mapped[Org] = relationship(back_populates="users")


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ak"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="default")
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------
# people
# --------------------------------------------------------------------------


class Candidate(Base, TimestampMixin):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("org_id", "source", "source_id", name="uq_candidate_source"),
        Index("ix_candidate_org_updated", "org_id", "updated_at"),
        # The CRM grid filters on both of these before it does anything else.
        Index("ix_candidate_org_status", "org_id", "crm_status"),
        Index("ix_candidate_org_tracked", "org_id", "crm_tracked"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cand"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)

    source: Mapped[str] = mapped_column(String(60), default="manual")
    source_id: Mapped[str] = mapped_column(String(200))

    full_name: Mapped[str] = mapped_column(String(200))
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    headline: Mapped[str] = mapped_column(String(400), default="")
    summary: Mapped[str] = mapped_column(Text, default="")

    location_city: Mapped[str] = mapped_column(String(120), default="")
    location_region: Mapped[str] = mapped_column(String(120), default="")
    location_country: Mapped[str] = mapped_column(String(120), default="")
    open_to_remote: Mapped[bool] = mapped_column(Boolean, default=False)

    linkedin_url: Mapped[str] = mapped_column(String(400), default="")
    github_url: Mapped[str] = mapped_column(String(400), default="")
    website_url: Mapped[str] = mapped_column(String(400), default="")

    emails: Mapped[list] = mapped_column(JSON, default=list)
    phones: Mapped[list] = mapped_column(JSON, default=list)

    skills: Mapped[list] = mapped_column(JSON, default=list)  # normalised
    skills_raw: Mapped[list] = mapped_column(JSON, default=list)
    industries: Mapped[list] = mapped_column(JSON, default=list)

    current_title: Mapped[str] = mapped_column(String(240), default="")
    current_title_normalized: Mapped[str] = mapped_column(String(240), default="")
    current_company: Mapped[str] = mapped_column(String(240), default="")
    # normalize_company(current_company), stamped by an ORM flush listener so
    # warm-contact lookups filter in SQL instead of loading the whole org.
    current_company_normalized: Mapped[str] = mapped_column(
        String(240), default="", server_default="", index=True
    )
    # Negative cache for the paid LinkedIn profile fetch (AGENTS.md house
    # rule): a real miss is stamped so it is never paid for twice; a provider
    # outage is NOT, so a quota wall cannot mark somebody unfetchable forever.
    profile_ingest_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    profile_ingest_error: Mapped[str] = mapped_column(String(400), default="", server_default="")
    current_company_domain: Mapped[str] = mapped_column(String(240), default="")

    seniority: Mapped[str] = mapped_column(String(40), default="unknown")
    years_experience: Mapped[float] = mapped_column(Float, default=0.0)

    doc_text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list | None] = mapped_column(EmbeddingVector, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(80), default="")

    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- CRM ---------------------------------------------------------------
    # Per-person status, deliberately distinct from ProjectCandidate.stage:
    # stage is per-role and partly outreach-driven, this is one hand-set fact
    # about the relationship itself. A plain string, not a Postgres enum, so
    # adding a value does not need its own migration.
    crm_status: Mapped[str] = mapped_column(String(32), default="none")
    # "Deliberately in the CRM", as opposed to merely sourced. Set only by
    # explicit CRM actions -- manual add, promoting a discovered contact,
    # setting a status or tag, and the first add to a project or sequence.
    # Never by an incidental profile edit: this widens the thread-sync privacy
    # guard in integrations/matching.py, so it must mean the user asked for
    # this person to be tracked, not that someone fixed their job title.
    crm_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Field names a human edited by hand. repository._apply re-applies these
    # on top of imported data so a nightly Foray refresh cannot silently
    # overwrite a correction.
    manual_fields: Mapped[list] = mapped_column(JSON, default=list)

    experiences: Mapped[list[Experience]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan", order_by="Experience.position"
    )
    educations: Mapped[list[Education]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )

    @property
    def primary_email(self) -> str | None:
        return self.emails[0] if self.emails else None


class Experience(Base):
    __tablename__ = "experiences"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("exp"))
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)

    company: Mapped[str] = mapped_column(String(240), default="")
    # normalize_company(company); the alumni half of warm-contact lookups.
    company_normalized: Mapped[str] = mapped_column(
        String(240), default="", server_default="", index=True
    )
    company_domain: Mapped[str] = mapped_column(String(240), default="")
    company_industry: Mapped[str] = mapped_column(String(120), default="")
    company_size: Mapped[str] = mapped_column(String(40), default="")  # e.g. "51-200"
    company_stage: Mapped[str] = mapped_column(String(40), default="")  # e.g. "series b"

    title: Mapped[str] = mapped_column(String(240), default="")
    title_normalized: Mapped[str] = mapped_column(String(240), default="")
    seniority: Mapped[str] = mapped_column(String(40), default="unknown")

    start_date: Mapped[str] = mapped_column(String(10), default="")  # YYYY-MM
    end_date: Mapped[str] = mapped_column(String(10), default="")
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    months: Mapped[int] = mapped_column(Integer, default=0)

    location: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")

    candidate: Mapped[Candidate] = relationship(back_populates="experiences")


class Education(Base):
    __tablename__ = "educations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("edu"))
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    school: Mapped[str] = mapped_column(String(240), default="")
    degree: Mapped[str] = mapped_column(String(120), default="")
    field_of_study: Mapped[str] = mapped_column(String(200), default="")
    start_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    candidate: Mapped[Candidate] = relationship(back_populates="educations")


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------

PIPELINE_STAGES = (
    "sourced",
    "contacted",
    "replied",
    "screening",
    "interviewing",
    "offer",
    "hired",
    "rejected",
)

# Stages that are driven by outreach state, not by hand: a candidate becomes
# "contacted" when a sequence step actually sends and "replied" when a reply
# lands (see outreach/sequencer.py::_advance_pipeline_stage). The pipeline board
# must not let a recruiter click through these; a manual touch outside the system
# is still possible via an explicit override.
OUTREACH_DRIVEN_STAGES = ("contacted", "replied")

# Per-person CRM status. Validated in the router the way stages are, rather
# than by a database enum, so the vocabulary can grow without a migration.
# "do_not_contact" is not decorative: setting it writes a Suppression, which
# the sequencer already honours on every send.
CRM_STATUSES = (
    "none",
    "prospect",
    "active",
    "nurture",
    "placed",
    "do_not_contact",
    "archived",
)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("proj"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    role_title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    # Client / hiring company. Copied onto the linked JobPosting so warm-path
    # matching can fire on our own mandates (Project otherwise has no company).
    company: Mapped[str] = mapped_column(String(240), default="")
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_by: Mapped[str | None] = mapped_column(String(40), nullable=True)

    members: Mapped[list[ProjectCandidate]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectCandidate(Base, TimestampMixin):
    __tablename__ = "project_candidates"
    __table_args__ = (
        UniqueConstraint("project_id", "candidate_id", name="uq_project_candidate"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pc"))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(32), default="sourced")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    added_by: Mapped[str | None] = mapped_column(String(40), nullable=True)

    project: Mapped[Project] = relationship(back_populates="members")
    candidate: Mapped[Candidate] = relationship()


# --------------------------------------------------------------------------
# crm
# --------------------------------------------------------------------------


class CandidateNote(Base, TimestampMixin):
    """A freeform note about a person.

    Distinct from ``ProjectCandidate.notes``, which is a per-role scratch field
    on a pipeline card: this one belongs to the person, survives them leaving a
    project, and records who wrote it and when.
    """

    __tablename__ = "candidate_notes"
    __table_args__ = (Index("ix_candidate_notes_candidate", "candidate_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("note"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    # Unconstrained, like Project.created_by: an API-key principal has no user,
    # and a deleted user must not take the note with them.
    author_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Snapshot of the byline, so the note still says who wrote it after that
    # user is gone.
    author_name: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


DISCOVERED_STATUSES = ("new", "already_known", "promoted", "dismissed")

# Why a staged contact looks worth reviving. Assigned from what is actually
# known -- notably NOT from thread_count, which counts our own follow-ups.
DISCOVERY_SUGGESTIONS = ("", "revive", "one_touch", "cc_only", "role_account", "suppressed")


class DiscoveredContact(Base, TimestampMixin):
    """Someone we have emailed who is not in Contacts yet.

    Evidence only: an address, a display name, counts and dates. There is no
    subject column and no body column, and there must never be one -- these are
    people who have not been reviewed, let alone opted into. "5 messages, last
    one 14 months ago" is enough to triage on. A test asserts the absence, so
    adding one fails loudly rather than quietly.

    Two mailboxes can meet the same stranger in the same sweep, so writes go
    through an upsert on ``(org_id, email)``; select-then-insert races.
    """

    __tablename__ = "discovered_contacts"
    __table_args__ = (
        UniqueConstraint("org_id", "email", name="uq_discovered_org_email"),
        Index("ix_discovered_org_status_seen", "org_id", "status", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("disc"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)

    email: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    domain: Mapped[str] = mapped_column(String(255), default="")

    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    outbound_count: Mapped[int] = mapped_column(Integer, default=0)
    to_count: Mapped[int] = mapped_column(Integer, default=0)
    cc_count: Mapped[int] = mapped_column(Integer, default=0)
    # Distinct conversations. A useful sort key, but NOT evidence of a reply:
    # sequence follow-ups share a conversation id with no answer at all.
    thread_count: Mapped[int] = mapped_column(Integer, default=0)

    mailbox_ids: Mapped[list] = mapped_column(JSON, default=list)

    status: Mapped[str] = mapped_column(String(20), default="new")
    suggested_status: Mapped[str] = mapped_column(String(24), default="")
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_reason: Mapped[str] = mapped_column(String(60), default="")


class CandidateEmail(Base):
    """One row per address a candidate owns: a projection of ``Candidate.emails``.

    ``emails`` is a JSON list, so "who owns this address?" cannot be a join --
    which is why ``build_address_index`` loads every in-scope candidate into a
    Python dict. That is tolerable once per sync job; it is not tolerable
    inside a single HTTP request, which is what the manual-add duplicate check
    and discovery's already-known test both need.

    ``UniqueConstraint(org_id, email)`` makes explicit the rule
    ``build_address_index`` currently only implies by iteration order: one
    address belongs to one person. Existing data can violate it -- nothing has
    ever stopped the same address landing in two JSON lists -- so the backfill
    is first-writer-wins and the loser keeps their JSON copy with no row here.
    The projection is therefore a subset of the JSON, never a mirror of it.

    Never a second source of truth: ``repository.sync_candidate_emails`` is the
    only writer, and it is called wherever ``emails`` changes.
    """

    __tablename__ = "candidate_emails"
    __table_args__ = (
        UniqueConstraint("org_id", "email", name="uq_candidate_email_org"),
        Index("ix_candidate_emails_candidate", "candidate_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cem"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(255))
    # 0 is the primary address, matching Candidate.primary_email.
    position: Mapped[int] = mapped_column(Integer, default=0)


class DuplicateDismissal(Base, TimestampMixin):
    """A pair someone has looked at and said are two different people.

    Suggestions are recomputed from live data every time, so without this the
    same "Are these the same John Smith?" comes back forever and the list
    stops being read. The pair is stored with the ids sorted, so it matches
    whichever way round it is later suggested.
    """

    __tablename__ = "duplicate_dismissals"
    __table_args__ = (
        UniqueConstraint("left_id", "right_id", name="uq_duplicate_dismissal"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("dup"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    left_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    right_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    dismissed_by: Mapped[str | None] = mapped_column(String(40), nullable=True)


class CandidateTag(Base, TimestampMixin):
    """A tag on a person.

    A join table rather than a JSON list on ``candidates``: tag filtering is a
    grid filter, and the ``skills`` column already demonstrates what happens
    when a filterable multi-value field is stored as JSON -- it cannot be
    indexed and ends up filtered in Python after paging.
    """

    __tablename__ = "candidate_tags"
    __table_args__ = (
        UniqueConstraint("candidate_id", "tag", name="uq_candidate_tag"),
        Index("ix_candidate_tags_org_tag", "org_id", "tag"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("tag"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    tag: Mapped[str] = mapped_column(String(60))
    created_by: Mapped[str | None] = mapped_column(String(40), nullable=True)


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


class Search(Base, TimestampMixin):
    __tablename__ = "searches"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("srch"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    raw_query: Mapped[str] = mapped_column(Text)
    parsed_query: Mapped[dict] = mapped_column(JSON, default=dict)
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    parser: Mapped[str] = mapped_column(String(32), default="heuristic")  # llm|heuristic
    reranked: Mapped[bool] = mapped_column(Boolean, default=False)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(40), nullable=True)

    results: Mapped[list[SearchResult]] = relationship(
        back_populates="search", cascade="all, delete-orphan", order_by="SearchResult.rank"
    )


class SearchResult(Base):
    __tablename__ = "search_results"
    __table_args__ = (Index("ix_search_result_search_rank", "search_id", "rank"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sres"))
    search_id: Mapped[str] = mapped_column(
        ForeignKey("searches.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer, default=0)
    lexical_score: Mapped[float] = mapped_column(Float, default=0.0)
    vector_score: Mapped[float] = mapped_column(Float, default=0.0)
    fusion_score: Mapped[float] = mapped_column(Float, default=0.0)
    heuristic_score: Mapped[float] = mapped_column(Float, default=0.0)
    rerank_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)

    search: Mapped[Search] = relationship(back_populates="results")
    candidate: Mapped[Candidate] = relationship()


# --------------------------------------------------------------------------
# outreach
# --------------------------------------------------------------------------


class Sequence(Base, TimestampMixin):
    __tablename__ = "sequences"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("seq"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="active")  # active|paused|archived
    personalize: Mapped[bool] = mapped_column(Boolean, default=True)
    stop_on_reply: Mapped[bool] = mapped_column(Boolean, default=True)
    from_email: Mapped[str] = mapped_column(String(255), default="")
    from_name: Mapped[str] = mapped_column(String(200), default="")
    # rotate: spread sends across every eligible mailbox in the org, honouring
    # each one's daily cap. fixed: send only from mailbox_id below.
    sender_mode: Mapped[str] = mapped_column(String(16), default="rotate")
    # The pinned mailbox when sender_mode is "fixed". Null under "rotate" with
    # no connected mailbox at all falls through to the global provider.
    mailbox_id: Mapped[str | None] = mapped_column(
        ForeignKey("mailbox_connections.id", ondelete="SET NULL"), nullable=True
    )

    steps: Mapped[list[SequenceStep]] = relationship(
        back_populates="sequence", cascade="all, delete-orphan", order_by="SequenceStep.step_order"
    )


class SequenceStep(Base):
    __tablename__ = "sequence_steps"
    __table_args__ = (UniqueConstraint("sequence_id", "step_order", name="uq_step_order"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("step"))
    sequence_id: Mapped[str] = mapped_column(
        ForeignKey("sequences.id", ondelete="CASCADE"), index=True
    )
    step_order: Mapped[int] = mapped_column(Integer, default=0)
    channel: Mapped[str] = mapped_column(String(32), default="email")
    delay_days: Mapped[int] = mapped_column(Integer, default=0)
    # Stored now, honoured by the scheduler in the send-windows workstream:
    # delay_unit days|business_days; send_at_time "HH:MM"; send_timezone IANA name.
    delay_unit: Mapped[str] = mapped_column(String(16), default="days")
    send_at_time: Mapped[str] = mapped_column(String(5), default="")
    send_timezone: Mapped[str] = mapped_column(String(64), default="")
    subject_template: Mapped[str] = mapped_column(Text, default="")
    body_template: Mapped[str] = mapped_column(Text, default="")

    sequence: Mapped[Sequence] = relationship(back_populates="steps")


class Enrollment(Base, TimestampMixin):
    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("sequence_id", "candidate_id", name="uq_enrollment_seq_candidate"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("enr"))
    sequence_id: Mapped[str] = mapped_column(
        ForeignKey("sequences.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="active")
    # active|completed|replied|stopped|bounced|failed
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_reason: Mapped[str] = mapped_column(String(120), default="")
    personalization: Mapped[dict] = mapped_column(JSON, default=dict)
    # Pinned at the first successful send and never reassigned: every step of
    # one conversation goes out from the same address, or the thread breaks in
    # the candidate's client. RESTRICT rather than SET NULL because silently
    # unpinning a live enrollment would let rotation move it mid-sequence --
    # disconnecting revokes the row instead of deleting it, so this never fires
    # in normal use.
    mailbox_id: Mapped[str | None] = mapped_column(
        ForeignKey("mailbox_connections.id", ondelete="RESTRICT"), nullable=True
    )

    sequence: Mapped[Sequence] = relationship()
    candidate: Mapped[Candidate] = relationship()


class Message(Base, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        # One outbound attempt per sequence step. Inbound replies share step_id
        # and must not collide. A crash after the provider accepted the mail
        # can still retry the provider; it cannot insert a second row.
        Index(
            "uq_messages_enrollment_step_outbound",
            "enrollment_id",
            "step_id",
            unique=True,
            sqlite_where=text("direction = 'outbound' AND step_id IS NOT NULL"),
            postgresql_where=text("direction = 'outbound' AND step_id IS NOT NULL"),
        ),
        # The per-candidate timeline sorts on sent_at; the native-id index
        # makes mailbox ingest idempotent (one row per provider message, ever).
        Index("ix_messages_candidate_sent", "candidate_id", "sent_at"),
        Index("ix_messages_org_conversation", "org_id", "conversation_id"),
        Index(
            "uq_messages_org_native_id",
            "org_id",
            "provider_native_id",
            unique=True,
            sqlite_where=text("provider_native_id != ''"),
            postgresql_where=text("provider_native_id != ''"),
        ),
        # Per-mailbox rolling send volume, for the rotation caps.
        Index("ix_messages_mailbox_sent", "mailbox_id", "sent_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("msg"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    # Nullable: mail synced from the user's own mailbox (source names the
    # provider) has no sequence behind it. Sequence sends always set it.
    enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("enrollments.id", ondelete="CASCADE"), index=True, nullable=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str | None] = mapped_column(
        ForeignKey("sequence_steps.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(32), default="email")
    direction: Mapped[str] = mapped_column(String(16), default="outbound")
    # "sequence" means the sequencer sent it. Anything else is the provider it
    # was synced from (microsoft | zoho) -- a manual email, or a reply captured
    # without a matching outbound.
    source: Mapped[str] = mapped_column(String(20), default="sequence")
    from_address: Mapped[str] = mapped_column(String(255), default="")
    to_address: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # queued|sent|failed|bounced|received
    error: Mapped[str] = mapped_column(Text, default="")
    provider_message_id: Mapped[str] = mapped_column(String(200), default="")
    # Which connected mailbox sent it, plus the ids reply detection matches on.
    # provider_native_id is the provider's own message id (Graph immutable id,
    # Zoho messageId) and is the ingest idempotency key -- see the partial
    # unique index above. conversation_id holds Graph's conversationId or
    # Zoho's threadId; they play the same role.
    mailbox_id: Mapped[str] = mapped_column(String(40), default="")
    provider_native_id: Mapped[str] = mapped_column(String(300), default="")
    internet_message_id: Mapped[str] = mapped_column(String(300), default="", index=True)
    conversation_id: Mapped[str] = mapped_column(String(200), default="")

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    click_count: Mapped[int] = mapped_column(Integer, default=0)

    reply_intent: Mapped[str] = mapped_column(String(40), default="")
    reply_text: Mapped[str] = mapped_column(Text, default="")
    # Resolved {{ai: ...}} fragments for this step, keyed by directive key.
    # Lives on the Message because (enrollment, step) is already the send's
    # idempotency key: a retried send must reuse the exact text that may have
    # already gone out, and the recruiter can audit what the model wrote.
    ai_fragments: Mapped[dict] = mapped_column(JSON, default=dict)
    # Thread-sync extras: the original HTML body when the plaintext is a
    # conversion, the provider's own deep link (the UI hands off rather than
    # rendering untrusted HTML), and how many attachments the message carried
    # (bytes are never stored).
    body_html: Mapped[str] = mapped_column(Text, default="")
    web_link: Mapped[str] = mapped_column(Text, default="")
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)

    enrollment: Mapped[Enrollment | None] = relationship()
    candidate: Mapped[Candidate] = relationship()


class MailboxConnection(Base, TimestampMixin):
    """A recruiter's own mailbox, connected via OAuth. Outreach sends as them.

    Tokens live in ``token_blob`` encrypted (integrations/crypto.py).

    Cursors come in two shapes on purpose. Graph hands back an opaque delta
    link per folder, so those keep their own columns. Zoho has no delta API and
    is polled by timestamp, so its high-water marks live in ``sync_state``
    alongside the folder ids it needs. Forcing one representation on both would
    have meant rewriting the working Outlook cursor to gain nothing.
    """

    __tablename__ = "mailbox_connections"
    __table_args__ = (UniqueConstraint("org_id", "address", name="uq_mailbox_org_address"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("mbx"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(40), default="microsoft")
    address: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    # The provider's own id for this mailbox: Graph user id, Zoho accountId.
    provider_account_id: Mapped[str] = mapped_column(String(120), default="")
    # Resolved at connect time, empty for providers with fixed hosts. Zoho only
    # reveals its data centre during the callback, and every later token
    # refresh must go back to the same accounts host.
    api_base: Mapped[str] = mapped_column(String(255), default="")
    auth_base: Mapped[str] = mapped_column(String(255), default="")
    token_blob: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    # active | error | revoked
    error: Mapped[str] = mapped_column(Text, default="")
    inbox_delta_link: Mapped[str] = mapped_column(Text, default="")
    sent_delta_link: Mapped[str] = mapped_column(Text, default="")
    # Provider-specific sync bookkeeping: Zoho folder ids, per-folder
    # high-water marks, and the last ids seen at that timestamp.
    sync_state: Mapped[dict] = mapped_column(JSON, default=dict)
    # Rotation controls. A cap of 0 or less makes the mailbox ineligible rather
    # than unlimited -- "stop using this one" is what a zero cap means.
    send_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_send_cap: Mapped[int] = mapped_column(Integer, default=40)
    # Opt-in for copying candidate correspondence (manual Outlook mail, both
    # directions) into the CRM timeline. Off by default: connecting a mailbox
    # for sending must not silently start retaining its contents.
    thread_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Sweep progress: {status, scanned, found, cursor, since, started_at,
    # finished_at, error}. It lives here rather than in the job payload because
    # the job re-enqueues itself per page, and a counter that resets every page
    # cannot show progress. The sweep cursor lives here too, and must never be
    # written to inbox_delta_link / sent_delta_link / sync_state["cursors"].
    discovery_state: Mapped[dict] = mapped_column(JSON, default=dict)
    # Progress of the per-candidate history import, same shape and same reason:
    # the job re-enqueues itself per batch, so a counter in the job payload
    # cannot survive to be shown. {status, done, total, imported, run, note,
    # started_at, finished_at, error}.
    backfill_state: Mapped[dict] = mapped_column(JSON, default=dict)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReplyDraft(Base, TimestampMixin):
    """An AI-written reply placed in the recruiter's own Drafts folder.

    One per inbound reply. ``body`` is what we generated; Phase 4.5 fills
    ``final_sent_text`` when the sent version shows up in Sent Items, which is
    the training signal for matching the recruiter's voice.
    """

    __tablename__ = "reply_drafts"
    __table_args__ = (
        UniqueConstraint("inbound_message_id", name="uq_reply_draft_inbound"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rdr"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    mailbox_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_connections.id", ondelete="CASCADE"), index=True
    )
    inbound_message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE")
    )
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    provider_draft_id: Mapped[str] = mapped_column(String(300), default="")
    web_link: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    generated_by: Mapped[str] = mapped_column(String(20), default="template")
    # llm | template
    intent: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(32), default="created")
    # created | sent | discarded
    final_sent_text: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Suppression(Base, TimestampMixin):
    """Org-level do-not-contact list.

    Emails are stored lowercased. Phone is E.164 when present. A STOP from an
    unknown number is phone-only, so ``email`` is nullable — two empty-email
    rows must not collide. Uniqueness: ``(org_id, email)`` when email is set,
    and ``(org_id, phone)`` when phone is non-empty (partial unique).
    """

    __tablename__ = "suppressions"
    __table_args__ = (
        UniqueConstraint("org_id", "email", name="uq_suppression_org_email"),
        Index(
            "uq_suppression_org_phone",
            "org_id",
            "phone",
            unique=True,
            sqlite_where=text("phone != ''"),
            postgresql_where=text("phone != ''"),
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sup"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str] = mapped_column(String(32), default="", server_default="")
    reason: Mapped[str] = mapped_column(String(40), default="unsubscribe")
    # unsubscribe | bounce | manual
    source: Mapped[str] = mapped_column(String(40), default="manual")
    # tracking | reply | bounce | api
    source_message_id: Mapped[str] = mapped_column(String(40), default="")


class Job(Base, TimestampMixin):
    """Durable work queue. The worker claims rows; the API never waits on them."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("org_id", "idempotency_key", name="uq_job_org_idempotency"),
        Index("ix_jobs_claim", "status", "run_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("job"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # queued | running | succeeded | failed | cancelled
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str] = mapped_column(String(80), default="")
    idempotency_key: Mapped[str] = mapped_column(String(200))
    last_error: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------
# agents + activity
# --------------------------------------------------------------------------


class SourcingAgent(Base, TimestampMixin):
    __tablename__ = "sourcing_agents"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("agent"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    query: Mapped[str] = mapped_column(Text)
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    min_score: Mapped[float] = mapped_column(Float, default=0.6)
    max_per_run: Mapped[int] = mapped_column(Integer, default=10)
    cadence_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    status: Mapped[str] = mapped_column(String(32), default="active")
    auto_enroll_sequence_id: Mapped[str | None] = mapped_column(
        ForeignKey("sequences.id", ondelete="SET NULL"), nullable=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_added: Mapped[int] = mapped_column(Integer, default=0)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_org_created", "org_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("evt"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(80), index=True)
    subject_type: Mapped[str] = mapped_column(String(40), default="")
    subject_id: Mapped[str] = mapped_column(String(40), default="")
    actor_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def record_event(
    session,
    *,
    org_id: str,
    type: str,
    subject_type: str = "",
    subject_id: str = "",
    actor_id: str | None = None,
    payload: dict | None = None,
) -> Event:
    evt = Event(
        org_id=org_id,
        type=type,
        subject_type=subject_type,
        subject_id=subject_id,
        actor_id=actor_id,
        payload=payload or {},
    )
    session.add(evt)
    return evt


# --------------------------------------------------------------------------
# jobs database (postings + candidate↔job matches)
# --------------------------------------------------------------------------


class JobPosting(Base, TimestampMixin):
    """An open-market or mandate-backed role, scored against CRM contacts.

    Separate from ``Job`` (the durable work queue) and from ``Project`` (our
    pipeline for a mandate). A Project upserts one of these with
    ``source="project"``; Exa/CSV/manual rows are the rest of the market.
    """

    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("org_id", "source_id", name="uq_job_posting_source_id"),
        Index("ix_job_posting_org_status", "org_id", "status"),
        Index("ix_job_postings_ats_vendor", "org_id", "ats_vendor"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("jpost"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    # Provenance of first sight: project | exa | manual | csv
    source: Mapped[str] = mapped_column(String(32), default="manual")
    # sha1 of a normalised URL, or of company|title|location, or "project:{id}".
    # Hashed so a URL longer than String(200) still fits the unique key.
    source_id: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(Text, default="")

    company: Mapped[str] = mapped_column(String(240), default="")
    company_normalized: Mapped[str] = mapped_column(String(240), default="")
    company_domain: Mapped[str] = mapped_column(String(240), default="")
    title: Mapped[str] = mapped_column(String(240), default="")
    title_normalized: Mapped[str] = mapped_column(String(240), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    open_to_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    seniority: Mapped[str] = mapped_column(String(40), default="")
    jd_text: Mapped[str] = mapped_column(Text, default="")

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # pending_parse | open | stale | closed | archived
    status: Mapped[str] = mapped_column(String(32), default="pending_parse")

    parsed_spec: Mapped[dict] = mapped_column(JSON, default=dict)
    spec_version: Mapped[str] = mapped_column(String(64), default="")
    parser: Mapped[str] = mapped_column(String(32), default="")
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding: Mapped[list | None] = mapped_column(EmbeddingVector, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(80), default="")

    # Which ATS is behind this posting and where its form is, resolved once by
    # applications/ats/detect.py and cached. Columns rather than `raw` because
    # the job card, the submitter and the recruiter surface all read it, and
    # `source_url` is frequently an aggregator listing rather than the form.
    ats_vendor: Mapped[str] = mapped_column(String(32), default="")
    ats_tenant: Mapped[str] = mapped_column(String(120), default="")
    ats_external_job_id: Mapped[str] = mapped_column(String(120), default="")
    apply_url: Mapped[str] = mapped_column(Text, default="")
    ats_resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    fee_bearing: Mapped[bool] = mapped_column(Boolean, default=False)
    fee_terms: Mapped[str] = mapped_column(String(240), default="")
    fee_evidence: Mapped[str] = mapped_column(Text, default="")
    # Cache of [{candidate_id, name, company, basis}]; repaired on the match sweep.
    referral_paths: Mapped[list] = mapped_column(JSON, default=list)
    has_warm_path: Mapped[bool] = mapped_column(Boolean, default=False)
    match_count: Mapped[int] = mapped_column(Integer, default=0)
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    matched_spec_version: Mapped[str] = mapped_column(String(64), default="")

    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class JobMatch(Base, TimestampMixin):
    """One candidate scored against one posting. Human status is sticky."""

    __tablename__ = "job_matches"
    __table_args__ = (
        UniqueConstraint("job_posting_id", "candidate_id", name="uq_job_match"),
        Index("ix_job_match_candidate_score", "org_id", "candidate_id", "score"),
        Index("ix_job_match_posting_score", "job_posting_id", "score"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("jmatch"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    job_posting_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    score: Mapped[float] = mapped_column(Float, default=0.0)
    heuristic_score: Mapped[float] = mapped_column(Float, default=0.0)
    vector_score: Mapped[float] = mapped_column(Float, default=0.0)
    criteria: Mapped[list] = mapped_column(JSON, default=list)
    # new | shortlisted | dismissed
    status: Mapped[str] = mapped_column(String(32), default="new")
    dismissed_reason: Mapped[str] = mapped_column(String(120), default="")
    spec_version: Mapped[str] = mapped_column(String(64), default="")
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobMatchWatermark(Base):
    """When this candidate was last scored, even if nothing cleared the floor.

    Lives off Candidate so the CRM workstream can keep owning that table.
    Distinguishes "scored and matched nothing" from "never scored".
    """

    __tablename__ = "job_match_watermarks"
    __table_args__ = (
        UniqueConstraint("org_id", "candidate_id", name="uq_job_match_watermark"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("jmw"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Bumped with JOB_MATCH_ENGINE_VERSION so a new term rescores the book
    # without nulling watermarks or touching Candidate.updated_at.
    engine_version: Mapped[str] = mapped_column(String(32), default="", server_default="")


class JobScrapeQuery(Base, TimestampMixin):
    """A saved Exa query that pulls job postings on a cadence."""

    __tablename__ = "job_scrape_queries"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("jscr"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(Text)
    cadence_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    status: Mapped[str] = mapped_column(String(32), default="active")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_found: Mapped[int] = mapped_column(Integer, default=0)
    # explicit | auto (title-family similar-jobs)
    origin: Mapped[str] = mapped_column(String(32), default="explicit")


# --------------------------------------------------------------------------
# voice intake
# --------------------------------------------------------------------------

# Where a call came from. The dial-in line is a different conversation from a
# first intake -- it updates a record that already exists -- so the entry point
# is stored rather than inferred from whether we recognised the number.
INTAKE_ENTRY_POINTS = ("web", "dial_in", "outbound")

# in_progress -> completed -> extracted, or failed/abandoned. "completed" means
# the call ended; "extracted" means we got data out of it. A call can complete
# and still yield nothing, and the two must be tellable apart when measuring.
INTAKE_CALL_STATUSES = (
    "in_progress",
    "completed",
    "extracted",
    "failed",
    "abandoned",
)


class IntakeCall(Base, TimestampMixin):
    """One voice intake call, and what came out of it.

    ``candidate_id`` is nullable because a stranger dialling the public number
    has no candidate row until we decide to create one, and the call has to be
    recorded before that decision -- otherwise a call we could not attribute
    leaves no trace at all, which is exactly the call worth looking at.

    ``org_id`` is resolved from the number that was dialled, or from a signed
    widget token, and never from the webhook body: the caller is unauthenticated
    and voice minutes are metered, so a payload-supplied org is both a
    cross-tenant write and somebody else's bill.
    """

    __tablename__ = "intake_calls"
    __table_args__ = (
        # The provider's own call id is the idempotency key. A webhook that is
        # retried -- and they are all retried -- must not extract twice.
        UniqueConstraint("provider", "provider_call_id", name="uq_intake_call_provider_id"),
        Index("ix_intake_calls_org_created", "org_id", "created_at"),
        Index("ix_intake_calls_org_review", "org_id", "review_needed"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("icall"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )

    provider: Mapped[str] = mapped_column(String(32), default="retell")
    provider_call_id: Mapped[str] = mapped_column(String(200))
    entry_point: Mapped[str] = mapped_column(String(16), default="web")

    # E.164 where we could produce one. ``to_number`` is what resolves the org
    # on an inbound call, so it is stored even when the caller is unknown.
    from_number: Mapped[str] = mapped_column(String(32), default="")
    to_number: Mapped[str] = mapped_column(String(32), default="")

    status: Mapped[str] = mapped_column(String(32), default="in_progress")
    # Recorded before any substantive question. Stored per call, not per person:
    # consent is given to a conversation, and two-party-consent jurisdictions
    # make "they agreed last time" the wrong answer.
    consent_recording: Mapped[bool] = mapped_column(Boolean, default=False)
    # TCPA consent to text this number, captured on the call where it was given.
    # Latched exactly like consent_recording. The verbatim exchange lives in
    # `transcript` on this same row, so the flag carries no copy of the
    # language -- the call row *is* the evidence record.
    sms_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)

    transcript: Mapped[str] = mapped_column(Text, default="")
    recording_url: Mapped[str] = mapped_column(Text, default="")

    # The provider payload, kept so an extraction can be replayed against a
    # better prompt without asking the person to call back.
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)
    extractor: Mapped[str] = mapped_column(String(32), default="")
    schema_version: Mapped[str] = mapped_column(String(32), default="")

    # Snapshot of the gate at hangup. Kept on the call rather than recomputed
    # from the record, because the record gets edited afterwards and this is
    # evidence about the call itself.
    required_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    fields_missing: Mapped[list] = mapped_column(JSON, default=list)
    low_confidence: Mapped[list] = mapped_column(JSON, default=list)
    review_needed: Mapped[bool] = mapped_column(Boolean, default=True)
    review_reason: Mapped[str] = mapped_column(String(400), default="")

    # Stated time budget, persisted on first sight of check_coverage's
    # minutes_available. None means today's full gate. A budget may only
    # narrow the required set (gate_mode=core); it never adds questions.
    minutes_available: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gate_mode: Mapped[str] = mapped_column(String(8), default="full", server_default="full")

    # What the caller said their name and email were. The inbound agent has
    # always asked for both -- "so a recruiter can find them" -- and until
    # these columns existed the answer was spoken, transcribed, and thrown
    # away, so every call from a number we did not already know landed
    # unattributed.
    #
    # Deliberately NOT in ``extracted``: that record is the preference record,
    # normalised through ``ParsedQuery``, and a name has no counterpart on the
    # job side. Same rule ``IntakeThread.contact_name`` follows, and the same
    # reason -- a ``contact_name`` inside ``spec`` is a field
    # ``jobmatch.engine`` cannot score.
    contact_name: Mapped[str] = mapped_column(String(240), default="", server_default="")
    contact_email: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # Required keys skipped by a core-mode trim. Empty is "never trimmed";
    # a listed key is unasked-on-purpose, not forgotten.
    deferred_fields: Mapped[list] = mapped_column(JSON, default=list)

    error: Mapped[str] = mapped_column(String(400), default="")


class CandidatePreference(Base, TimestampMixin):
    """What a candidate says they want, in the vocabulary a job is parsed into.

    The mirror of ``JobPosting.parsed_spec``: ``spec`` holds the same
    ``ParsedQuery`` shape, normalised through the same taxonomy, so
    ``jobmatch.engine`` can score a posting's requirements against a person's
    stated wants instead of only the other way round.

    Its own table rather than columns on ``Candidate`` for two reasons.
    ``repository._apply`` re-applies ``manual_fields`` over imported data
    specifically so a nightly Foray refresh cannot overwrite a human
    correction; preferences need that same immunity, and widening
    ``manual_fields`` to cover twenty more columns makes an already subtle
    mechanism carry weight it was not built for. And a preference is a
    statement made at a point in time by a person, not a fact about them --
    ``source_call_id`` says which conversation it came from.

    Booleans here are nullable on purpose. ``Candidate.open_to_remote`` cannot
    tell "wants an office" from "never asked", and that ambiguity is the reason
    a filter on it is untrustworthy. Three states, always.
    """

    __tablename__ = "candidate_preferences"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_candidate_preference"),
        Index("ix_candidate_preferences_org", "org_id"),
        Index("ix_candidate_preferences_org_review", "org_id", "review_needed"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cpref"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    source_call_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_calls.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Sibling of source_call_id rather than a polymorphic source_type/source_id
    # pair: a real FK per channel keeps the cascade and the join honest, and
    # exactly one of the two is set. Both are SET NULL, so deleting the
    # conversation drops the provenance and keeps the preferences.
    source_thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_threads.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # ParsedQuery-shaped, same as JobPosting.parsed_spec.
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    schema_version: Mapped[str] = mapped_column(String(32), default="")

    employment_status: Mapped[str] = mapped_column(String(32), default="")
    reason_for_looking: Mapped[str] = mapped_column(Text, default="")
    timeline: Mapped[str] = mapped_column(String(32), default="")
    urgency_driver: Mapped[str] = mapped_column(Text, default="")

    target_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    # "UNKNOWN" when a figure was given without one. Assuming USD silently is
    # how a 90k GBP expectation becomes a 90k USD shortlist.
    comp_currency: Mapped[str] = mapped_column(String(8), default="")
    comp_flexibility_notes: Mapped[str] = mapped_column(Text, default="")
    equity_importance: Mapped[str] = mapped_column(String(16), default="")

    work_authorization: Mapped[str] = mapped_column(String(120), default="")
    willing_to_relocate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    active_processes: Mapped[list] = mapped_column(JSON, default=list)
    offers_in_hand: Mapped[list] = mapped_column(JSON, default=list)
    # Verbatim: a client responds to the candidate's own words, and the point of
    # asking was to get phrasing a resume does not have.
    highlights: Mapped[list] = mapped_column(JSON, default=list)

    # Never rendered in a client-facing view. Stored so a recruiter knows what
    # not to say, which means the guard belongs at every read site.
    confidential_items: Mapped[list] = mapped_column(JSON, default=list)
    ok_to_share_with_clients: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Asked and refused. Distinct from empty: it stops the agent asking again on
    # the next call, and stops a recruiter reading silence as an oversight.
    declined: Mapped[list] = mapped_column(JSON, default=list)
    low_confidence: Mapped[list] = mapped_column(JSON, default=list)
    fields_missing: Mapped[list] = mapped_column(JSON, default=list)
    review_needed: Mapped[bool] = mapped_column(Boolean, default=True)
    review_reason: Mapped[str] = mapped_column(String(400), default="")
    # Set when a human -- recruiter or the candidate on the review page -- has
    # confirmed the record. Until then it is a machine reading of a phone call.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[str] = mapped_column(String(32), default="")
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IntakeFieldAnswer(Base, TimestampMixin):
    """Per-field provenance for one call. Not a second source of truth.

    ``jobmatch`` reads ``CandidatePreference`` only. This table exists so a
    recruiter can see the sentence a value came from, and so ``asked=false``
    distinguishes a core-mode trim from a field the agent forgot.
    """

    __tablename__ = "intake_field_answers"
    __table_args__ = (
        UniqueConstraint("intake_call_id", "field_key", name="uq_intake_field_answer_call_key"),
        Index("ix_intake_field_answers_org_call", "org_id", "intake_call_id"),
        Index("ix_intake_field_answers_candidate", "org_id", "candidate_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ifan"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    intake_call_id: Mapped[str] = mapped_column(
        ForeignKey("intake_calls.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    field_key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict | list | str | float | bool | None] = mapped_column(JSON, nullable=True)
    verbatim: Mapped[str] = mapped_column(Text, default="", server_default="")
    confidence: Mapped[str] = mapped_column(String(16), default="", server_default="")
    declined: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    asked: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class CandidatePhone(Base):
    """One row per number a candidate owns: a projection of ``Candidate.phones``.

    The same shape as ``CandidateEmail`` and for the same reason -- ``phones`` is
    a JSON list, so "who is calling?" cannot be a join -- but with a sharper
    failure mode. A wrong email lookup finds nobody. A wrong *phone* lookup
    finds the wrong person, and the dial-in agent then reads their comp
    expectations and interview pipeline to whoever dialled.

    So the key is E.164 from ``intake.phones.lookup_key``, which is stricter
    than ``clean_phones``: it refuses to guess a country code rather than
    approximate one. A stored number that cannot produce a key simply has no row
    here, which makes this projection a strict subset of the JSON in the same way
    ``candidate_emails`` is.

    ``UniqueConstraint(org_id, phone)`` states the rule the lookup depends on:
    one number belongs to one person. Existing data can violate it, so the
    backfill is first-writer-wins and the loser keeps their JSON copy with no
    row here.
    """

    __tablename__ = "candidate_phones"
    __table_args__ = (
        UniqueConstraint("org_id", "phone", name="uq_candidate_phone_org"),
        Index("ix_candidate_phones_candidate", "candidate_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cph"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    # E.164, e.g. +14155550123.
    phone: Mapped[str] = mapped_column(String(32))
    # 0 is the number Candidate.phones lists first.
    position: Mapped[int] = mapped_column(Integer, default=0)


class IntakeThread(Base, TimestampMixin):
    """One text intake conversation, however long it stays open.

    A sibling of ``IntakeCall`` rather than a ``channel`` column on it. A call
    is a bounded session with a duration, a recording, and a transcript that
    arrives once at the end. A thread has many inbound events, can go dormant
    for a week and resume, and never has a duration -- folding both into one
    table leaves half its columns permanently null on either side. Unifying
    them behind an ``IntakeSession`` is a fair refactor after the voice branch
    lands; before that, a rename is a guaranteed merge conflict.

    ``record`` is the load-bearing difference. A call extracts once, from a
    finished transcript. A thread has to answer "what do we still owe?" before
    *every* outbound message, so the merged record lives here and is rewritten
    each turn. It is the shape ``intake.schema.empty_record()`` produces, so
    ``coverage()`` reads it with no adapter.

    ``org_id`` is resolved from a signed widget token or the number that was
    texted, never from the request body -- the person on the other end is
    unauthenticated, and a payload-supplied org is a cross-tenant write.
    """

    __tablename__ = "intake_threads"
    __table_args__ = (
        # Same contract as intake_calls.provider_call_id, for the same reason:
        # inbound webhooks are retried. Partial because the web channel has no
        # external conversation id to supply, and blank must not collide with
        # blank.
        Index(
            "uq_intake_threads_provider_id",
            "provider",
            "provider_thread_id",
            unique=True,
            sqlite_where=text("provider_thread_id != ''"),
            postgresql_where=text("provider_thread_id != ''"),
        ),
        Index("ix_intake_threads_org_status", "org_id", "status"),
        Index("ix_intake_threads_org_created", "org_id", "created_at"),
        # How an inbound text finds the thread it belongs to, and how a STOP
        # from a stranger is remembered when there is no candidate to suppress.
        Index("ix_intake_threads_channel_from", "channel", "from_address"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ithread"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    # Nullable for the same reason IntakeCall's is: a stranger who opens the
    # widget has no candidate row, and the conversation has to be recorded
    # before we decide whether to create one.
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # web | sms | linq. A plain string, not an enum, so a new channel needs no
    # migration -- IntakeCall.provider is a string for the same reason.
    channel: Mapped[str] = mapped_column(String(16), default="web")
    provider: Mapped[str] = mapped_column(String(32), default="native")
    provider_thread_id: Mapped[str] = mapped_column(String(200), default="")
    entry_point: Mapped[str] = mapped_column(String(24), default="web_widget")
    # full | linkedin_short. Linq defaults to linkedin_short; web stays on the
    # original eleven-field gate unless a caller opts in.
    flow: Mapped[str] = mapped_column(String(32), default="full")

    # E.164 for sms, empty for web. to_address is what resolves the org on an
    # inbound text, so it is stored even when the sender is unknown.
    #
    # 255, not 64: an iMessage sender is not always a phone number. Apple ID
    # email addresses arrive here too, and a truncated sender address is a
    # thread that can never be found again -- including for the STOP lookup,
    # which is the one that must never fail.
    from_address: Mapped[str] = mapped_column(String(255), default="")
    to_address: Mapped[str] = mapped_column(String(64), default="")

    # active | complete | stopped. Dormancy is deliberately not a status: it is
    # derived from last_inbound_at, so there is no state to keep in step with
    # the clock and a thread cannot be "dormant" and freshly answered at once.
    status: Mapped[str] = mapped_column(String(24), default="active")
    # intake (the original coverage-gate interview; may complete) or the
    # candidate-agent modes: onboarding -> profile -> managing. A managing
    # thread is permanent -- only mode="intake" ever reaches STATUS_COMPLETE,
    # because run_turn is silent forever on a complete thread and a recruiting
    # manager's line going permanently dead is a product failure, not an end
    # state. Default preserves every existing thread byte-for-byte.
    mode: Mapped[str] = mapped_column(String(24), default="intake", server_default="intake")
    # The profile URL the candidate handed over during onboarding.
    linkedin_url: Mapped[str] = mapped_column(String(400), default="", server_default="")
    # The cards most recently shown: [{proposal_id, posting_id, company,
    # title}]. What "the Goldman one" resolves against. Reassigned, never
    # mutated -- the JSON-tracking rule threads.py documents. Nullable because
    # ALTER ADD COLUMN NOT NULL needs a server default and SQLite/Postgres
    # disagree on JSON defaults; None reads as "nothing shown yet".
    last_presented: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    presented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # TCPA: prior express consent before the first outbound text. On web the
    # person opened the widget and typed, which is the consent; on SMS it has
    # to come from somewhere else, and the channel refuses to send without it.
    consent_messaging: Mapped[bool] = mapped_column(Boolean, default=False)

    # Identity, asked in the thread and kept off the preference spec on
    # purpose: a name is not a hiring preference and ParsedQuery has no field
    # for one. These feed the candidate upsert once the gate closes.
    contact_name: Mapped[str] = mapped_column(String(240), default="")
    contact_email: Mapped[str] = mapped_column(String(255), default="")
    contact_linkedin: Mapped[str] = mapped_column(String(400), default="")

    # The merged intake record, same shape as intake.schema.empty_record().
    record: Mapped[dict] = mapped_column(JSON, default=dict)
    extractor: Mapped[str] = mapped_column(String(32), default="")
    schema_version: Mapped[str] = mapped_column(String(32), default="")

    required_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    fields_missing: Mapped[list] = mapped_column(JSON, default=list)
    low_confidence: Mapped[list] = mapped_column(JSON, default=list)
    review_needed: Mapped[bool] = mapped_column(Boolean, default=True)
    review_reason: Mapped[str] = mapped_column(String(400), default="")

    # The field the last outbound message asked about, and how many times each
    # field has been asked. Both exist to stop the one failure mode that gets a
    # number blocked: asking the same question again because the extractor
    # could not read the answer.
    last_field_asked: Mapped[str] = mapped_column(String(64), default="")
    asked_counts: Mapped[dict] = mapped_column(JSON, default=dict)

    # Send-window zone (IANA name) and how it was resolved -- see
    # messaging/cadence.py. Stored, not recomputed, so the window applied to a
    # send is auditable: "stated_location" and "candidate_record" carry
    # different confidence, and empty means the conservative unknown-zone
    # window (11:00-21:00 America/New_York) was in force.
    timezone: Mapped[str] = mapped_column(String(64), default="")
    timezone_source: Mapped[str] = mapped_column(String(24), default="")

    inbound_count: Mapped[int] = mapped_column(Integer, default=0)
    outbound_count: Mapped[int] = mapped_column(Integer, default=0)
    last_inbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_outbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error: Mapped[str] = mapped_column(String(400), default="")


class IntakeMessage(Base, TimestampMixin):
    """One message in an intake thread, either direction.

    ``seq`` orders the thread, not ``created_at``: two rows written in the same
    transaction share a timestamp to the microsecond on some backends, and a
    transcript assembled in the wrong order changes what the extractor reads.
    """

    __tablename__ = "intake_messages"
    __table_args__ = (
        # The sender's own message id is the idempotency key -- the precedent
        # is Message.provider_native_id, partial index included, because our
        # own outbound rows have no provider id and blank is not unique.
        #
        # Scoped to the thread, not the org as Message is. A mail provider's
        # message id is unique across a tenant, but a web chat's id comes from
        # the browser: two strangers both sending "1" would have the second
        # message silently swallowed as a duplicate of the first. Thread scope
        # is safe for a vendor id too, since one always arrives on its own
        # thread.
        Index(
            "uq_intake_messages_thread_native_id",
            "thread_id",
            "provider_native_id",
            unique=True,
            sqlite_where=text("provider_native_id != ''"),
            postgresql_where=text("provider_native_id != ''"),
        ),
        UniqueConstraint("thread_id", "seq", name="uq_intake_message_seq"),
        Index("ix_intake_messages_thread", "thread_id", "seq"),
        # Status webhooks (delivered/read) arrive with Linq's message id and no
        # thread id we trust, so lookup cannot go through the thread-scoped
        # unique index above.
        Index(
            "ix_intake_messages_native_id",
            "provider_native_id",
            sqlite_where=text("provider_native_id != ''"),
            postgresql_where=text("provider_native_id != ''"),
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("imsg"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    # No index=True: ix_intake_messages_thread below leads on thread_id and
    # serves the same lookups, and a thread is always read in seq order.
    thread_id: Mapped[str] = mapped_column(ForeignKey("intake_threads.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, default=0)

    direction: Mapped[str] = mapped_column(String(16), default="inbound")
    body: Mapped[str] = mapped_column(Text, default="")
    provider_native_id: Mapped[str] = mapped_column(String(300), default="")

    # Which intake field this outbound message asked about; blank on inbound
    # and on anything that is not a question.
    asked_field: Mapped[str] = mapped_column(String(64), default="")
    # reply (answers something inbound) | proactive (nudge, proposal, notify)
    # | system (STOP acknowledgement, HELP). Budgets in messaging/cadence.py
    # count proactive rows, so the budget cannot drift from what was sent.
    origin: Mapped[str] = mapped_column(String(16), default="reply")
    # queued | sent | delivered | read | failed | received
    status: Mapped[str] = mapped_column(String(24), default="received")
    error: Mapped[str] = mapped_column(String(400), default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class IntakeNumber(Base, TimestampMixin):
    """A phone number we own, and the org it belongs to.

    **This table is how a webhook gets an org.** A candidate on a phone call has
    no principal -- ``get_principal`` has nothing to read -- so the org has to
    come from something the caller cannot choose. The number that was dialled is
    that something; the request body is not. An org id taken from a webhook
    payload is a cross-tenant write and somebody else's phone bill, which is the
    same mistake ``AGENTS.md`` records against Foray's paid routes.

    ``phone`` is unique **globally**, not per org. A real-world number belongs to
    exactly one org, and scoping the constraint per org would let two orgs claim
    the same number and race for its calls.

    Web calls have no dialled number. They resolve instead by ``call_id`` against
    an ``IntakeCall`` row we created when we placed the call -- the same shape as
    ``routers/track.py`` reading ``message.org_id`` off a row rather than a query
    string.
    """

    __tablename__ = "intake_numbers"
    __table_args__ = (
        UniqueConstraint("phone", name="uq_intake_number_phone"),
        Index("ix_intake_numbers_org_active", "org_id", "active"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("inum"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="retell")
    # E.164, from intake.phones.lookup_key.
    phone: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(120), default="")

    # Which agent answers, and which one dials out. Retell allows one of each
    # per number, which is exactly the two conversations: inbound is a status
    # update against an existing record, outbound is a first full intake.
    inbound_agent_id: Mapped[str] = mapped_column(String(120), default="")
    outbound_agent_id: Mapped[str] = mapped_column(String(120), default="")

    # Turning a number off must not delete its call history, and a deleted row
    # would make every past IntakeCall unattributable.
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ConsentRecord(Base, TimestampMixin):
    """Consent provenance: who agreed to what, where, and what they were shown.

    CANDIDATE_AGENT_PLAN §2: with sourced outbound in scope, the artifact *is*
    the defence in a dispute. One row per grant, written wherever consent is
    obtained — a widget opened, an inbound text, a voice wrap-up opt-in — and
    never updated in place. Revocation is its own fact (``revoked_at``); the
    suppression list remains what send paths consult, this is the audit trail
    behind it.

    ``disclosure_text`` and ``disclosure_version`` are stored verbatim because
    the wording changes over time and the defence rests on what *this person*
    was shown, not on what the template says today.
    """

    __tablename__ = "consent_records"
    __table_args__ = (
        Index("ix_consent_records_org_candidate", "org_id", "candidate_id"),
        Index("ix_consent_records_thread", "thread_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("crec"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_threads.id", ondelete="SET NULL"), nullable=True
    )
    call_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_calls.id", ondelete="SET NULL"), nullable=True
    )

    # web | imessage | sms | voice | email
    channel: Mapped[str] = mapped_column(String(24), default="")
    # messaging (this conversation may continue) | application_updates (texts
    # about applications, the 10DLC use case). Never "marketing" — nothing in
    # the product asks for that, so nothing can claim it.
    scope: Mapped[str] = mapped_column(String(32), default="messaging")
    # web_widget | inbound_text | voice_wrap_up | portal
    source: Mapped[str] = mapped_column(String(32), default="")

    disclosure_text: Mapped[str] = mapped_column(Text, default="")
    disclosure_version: Mapped[str] = mapped_column(String(40), default="")
    # The exact words the person used, when there are any (a voice opt-in's
    # "yes, that's fine"). Empty for consent-by-conduct like opening a widget.
    verbatim: Mapped[str] = mapped_column(String(400), default="")

    granted: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The acting principal for recruiter-recorded consent; empty when the
    # person granted it themselves on an unauthenticated surface.
    actor_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


# --------------------------------------------------------------------------
# candidate agent: proposals, applications, documents
# --------------------------------------------------------------------------

# CANDIDATE_AGENT_PLAN §10. Forward flow plus the honest escapes; the legal
# transitions live in applications/state.py, validated in code like every
# other status vocabulary here.
#
#   draft            created; package not yet built
#   prepared         tailored resume / cover letter / answers exist
#   awaiting_consent the package was shown; the per-application yes is pending
#   submitting       an agent attempt is in flight (Tier 1-3); on failure it
#                    falls back to awaiting_consent with a status_reason
#   submitted        it was sent (candidate one-tap, recruiter, or agent on a
#                    tier that allows it) -- REQUIRES a recorded consent
#   acknowledged     the employer confirmed receipt
#   interviewing / offer / rejected / withdrawn
APPLICATION_STATUSES = (
    "draft",
    "prepared",
    "awaiting_consent",
    # An attempt is in flight. Only the agent path passes through here: it is
    # what stops an automated submission reaching `submitted` via the
    # prepared -> submitted shortcut, which exists for a human's "apply to 2".
    "submitting",
    "submitted",
    "acknowledged",
    "interviewing",
    "offer",
    "rejected",
    "withdrawn",
)
APPLICATION_TERMINAL_STATUSES = ("rejected", "withdrawn")

JOB_PROPOSAL_STATUSES = ("proposed", "accepted", "declined", "expired")

DOCUMENT_KINDS = ("resume_uploaded", "resume_generated", "cover_letter", "other")
DOCUMENT_STATUSES = ("uploaded", "generating", "ready", "failed")


class JobProposal(Base, TimestampMixin):
    """One posting put in front of one candidate, and what they said.

    **Deliberately not a status on ``JobMatch``.** That field is recruiter
    triage (new | shortlisted | dismissed); candidate consent is a different
    axis, and collapsing them means a recruiter shortlisting something reads
    as the candidate agreeing to it (CANDIDATE_AGENT_PLAN §10). The portal
    feed and the message thread read *this* table; the recruiter board reads
    matches. The two meet only in provenance columns.

    Unique per ``(candidate_id, job_posting_id)``: a role passed on is not
    re-surfaced, on any channel -- silence and swipes-left are information.
    """

    __tablename__ = "job_proposals"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "job_posting_id", name="uq_job_proposal_candidate_posting"
        ),
        Index("ix_job_proposals_org_candidate", "org_id", "candidate_id"),
        Index("ix_job_proposals_org_status", "org_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("jprop"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    job_posting_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), index=True
    )
    job_match_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_matches.id", ondelete="SET NULL"), nullable=True
    )

    # imessage | web | portal -- where it was shown. A proposal in a text and
    # a card swiped in the portal are the same fact on different surfaces.
    channel: Mapped[str] = mapped_column(String(24), default="")
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_threads.id", ondelete="SET NULL"), nullable=True
    )
    # The outbound message that carried it, when a thread did.
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_messages.id", ondelete="SET NULL"), nullable=True
    )

    # proposed | accepted | declined | expired
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    # The evidence strings shown with the card -- what we told them the fit
    # was, frozen at proposal time. Never a score (§5).
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The inbound message that answered, when the answer came by text.
    responded_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_messages.id", ondelete="SET NULL"), nullable=True
    )
    decline_reason: Mapped[str] = mapped_column(String(240), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Application(Base, TimestampMixin):
    """One pursuit of one posting on behalf of one candidate.

    The lifecycle after a proposal is accepted: prepare the package, get the
    per-application yes, a human submits, statuses arrive. ``JobMatch`` is
    triage, ``JobProposal`` is presentation and consent-to-pursue -- this row
    is the pursuit itself, and the only place a submission can hang off.

    **Consent is structural.** ``applications/state.py`` refuses the
    transition to ``submitted`` unless ``consenting_message_id`` or
    ``approved_at`` is set: a candidate saying "yes, things like that" is not
    consent to a specific submission, and this table is what keeps us out of
    "you applied to forty jobs as me" (§4).

    Documents hang off ``CandidateDocument.application_id`` rather than FK
    columns here -- one less cycle, and "the current resume variant" is the
    latest ready row per kind.
    """

    __tablename__ = "applications"
    __table_args__ = (
        # Exactly one application per (candidate, posting), ever. A duplicate
        # submission reads as spam in an ATS and is the single most
        # embarrassing failure available to us (§4).
        UniqueConstraint(
            "candidate_id", "job_posting_id", name="uq_application_candidate_posting"
        ),
        Index("ix_applications_org_status", "org_id", "status"),
        Index("ix_applications_org_candidate", "org_id", "candidate_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("app"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    job_posting_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), index=True
    )
    # Provenance, not state: which match and which proposal led here. Creating
    # an application never mutates JobMatch.status.
    job_match_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_matches.id", ondelete="SET NULL"), nullable=True
    )
    proposal_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_proposals.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )

    # candidate | recruiter | agent -- who started the pursuit.
    source: Mapped[str] = mapped_column(String(24), default="candidate")
    status: Mapped[str] = mapped_column(String(32), default="draft")
    status_reason: Mapped[str] = mapped_column(String(240), default="")

    # The per-application yes, bound to the message it arrived in (a text) or
    # to a portal approval. One of these must be set before `submitted`.
    consenting_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_messages.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_verbatim: Mapped[str] = mapped_column(String(400), default="")

    # Where and how a human applies. Tier 0 is a prepared package plus this
    # link; the copy must never imply we submitted somewhere we did not (§8).
    submission_url: Mapped[str] = mapped_column(Text, default="")
    # portal_link | recruiter_manual | email | ats_api
    submission_method: Mapped[str] = mapped_column(String(32), default="")
    # candidate | recruiter | agent
    submitted_by: Mapped[str] = mapped_column(String(16), default="")
    submitted_by_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # [{key, question, answer, provenance: {kind, ref}, generated_by, edited}]
    # -- every answer names the resume line, preference field, or message that
    # supports it. No answer is generated from nothing (§8).
    form_answers: Mapped[list] = mapped_column(JSON, default=list)
    # Cached warm-contact suggestions + drafted intros for this application.
    networking: Mapped[list] = mapped_column(JSON, default=list)

    package_error: Mapped[str] = mapped_column(String(400), default="")

    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitting_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(40), nullable=True)


class CandidateDocument(Base, TimestampMixin):
    """A file that belongs to a candidate: an uploaded resume, a generated one,
    a cover letter.

    Metadata only -- the bytes live in the local document store behind
    ``storage.py``, path-addressed by ids (``storage_path``). ``structured``
    is the ``resume.v1`` JSON: the parse result for uploads, the render source
    for generated documents. Parsed facts land HERE first and are promoted to
    the Candidate by an explicit merge step, never by a blind upsert --
    ``repository._replace_children`` rebuilds experience rows wholesale, and
    ``Candidate.summary``/``skills`` feed the search haystack (§7).

    ``application_id`` set = a per-application tailored variant; null = a
    profile-level document. ``is_default`` marks the profile resume.
    """

    __tablename__ = "candidate_documents"
    __table_args__ = (
        Index("ix_candidate_documents_candidate", "org_id", "candidate_id"),
        Index("ix_candidate_documents_application", "application_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cdoc"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[str | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), nullable=True
    )

    # resume_uploaded | resume_generated | cover_letter | other
    kind: Mapped[str] = mapped_column(String(32), default="other")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    # The original upload name, display only -- storage paths are id-built and
    # never touch this.
    filename: Mapped[str] = mapped_column(String(255), default="")
    content_type: Mapped[str] = mapped_column(String(100), default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    storage_path: Mapped[str] = mapped_column(String(400), default="")

    # uploaded | generating | ready | failed
    status: Mapped[str] = mapped_column(String(24), default="ready")
    text_extracted: Mapped[str] = mapped_column(Text, default="")
    structured: Mapped[dict] = mapped_column(JSON, default=dict)
    schema_version: Mapped[str] = mapped_column(String(32), default="")
    # llm | template | "" (an upload) -- the ReplyDraft convention.
    generated_by: Mapped[str] = mapped_column(String(20), default="")
    # upload | linkedin_ingest | generated -- where the content came from.
    source: Mapped[str] = mapped_column(String(32), default="")
    # The thread it arrived on, when one did.
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_threads.id", ondelete="SET NULL"), nullable=True
    )

    error: Mapped[str] = mapped_column(String(400), default="")
    created_by: Mapped[str | None] = mapped_column(String(40), nullable=True)


#: One submission attempt's own lifecycle, independent of the application's.
#: `needs_verification` parks a browser run waiting on mail to the alias;
#: `needs_human` is a terminus, not a retry -- a required field we could not
#: answer from the record is not something to guess at on someone's behalf.
SUBMISSION_STATUSES = (
    "queued",
    "running",
    "needs_verification",
    "needs_human",
    "succeeded",
    "failed",
)
SUBMISSION_METHODS = ("email", "ats_api", "browser", "console", "memory")


class ApplicationSubmission(Base, TimestampMixin):
    """One attempt to actually send one application.

    Separate from ``Application`` because an attempt is not a state of the
    pursuit: a Workday run can park on an email verification, fail on a changed
    form, and succeed on the retry, and all three are facts worth keeping. The
    application's own status moves ``awaiting_consent -> submitting ->
    submitted`` over the top of them.

    ``artifacts`` holds the storage-relative paths of whatever the adapter
    recorded -- for a browser run, the final screenshot and the field map it
    filled. A submission nobody can reconstruct is one we cannot answer for
    (CANDIDATE_AGENT_PLAN §8).
    """

    __tablename__ = "application_submissions"
    __table_args__ = (
        UniqueConstraint("application_id", "attempt", name="uq_application_submission_attempt"),
        Index("ix_application_submissions_org_status", "org_id", "status"),
        Index("ix_application_submissions_application", "application_id"),
        # At most one succeeded attempt per application, enforced where two
        # racing workers cannot talk themselves out of it. The uniqueness on
        # (candidate, posting) stops a second application; this stops a second
        # submission of the one application.
        Index(
            "uq_application_submission_succeeded",
            "application_id",
            unique=True,
            sqlite_where=text("status = 'succeeded'"),
            postgresql_where=text("status = 'succeeded'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("asub"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    vendor: Mapped[str] = mapped_column(String(32), default="")
    # email | ats_api | browser | console | memory
    method: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # The vendor's own id for the application, when it hands one back, and
    # whatever a human would quote at the employer to find it.
    external_id: Mapped[str] = mapped_column(String(200), default="")
    confirmation_ref: Mapped[str] = mapped_column(String(400), default="")
    error: Mapped[str] = mapped_column(String(600), default="")
    # Storage-relative paths: screenshots, the field map, a saved response.
    artifacts: Mapped[list] = mapped_column(JSON, default=list)
    # Verification state for a parked browser run: what we are waiting for and
    # what arrived at the alias.
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AtsBoardCredential(Base, TimestampMixin):
    """An employer's own API key for their job board.

    Greenhouse, Lever, Ashby and SmartRecruiters all publish an apply API, and
    all of them authenticate with a key the **employer** generates inside their
    own account. There is no key that spans boards -- one company's key does not
    post to another's -- so a single global setting was never the right shape
    for this and briefly was.

    Holding a key IS the relationship. When we have one, the API is the better
    path: structured errors, a real application id back, no browser. When we do
    not -- the open market, which is most postings -- the candidate's own door
    is the hosted form, and ``submit/browser/hosted_form.py`` drives it.

    In practice a key arrives with a mandate: a client whose roles we are
    filling (``JobPosting.project_id`` set) is who you can reasonably ask.
    """

    __tablename__ = "ats_board_credentials"
    __table_args__ = (
        UniqueConstraint("org_id", "vendor", "tenant", name="uq_ats_board_credential"),
        Index("ix_ats_board_credentials_org_vendor", "org_id", "vendor"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("atsbc"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    vendor: Mapped[str] = mapped_column(String(32))
    #: The employer's namespace inside the vendor: a Greenhouse board token, a
    #: Lever site, an Ashby org.
    tenant: Mapped[str] = mapped_column(String(120))
    #: What a human calls this, for the settings screen that will exist later.
    label: Mapped[str] = mapped_column(String(120), default="")
    token_blob: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(40), nullable=True)


class CandidateAlias(Base, TimestampMixin):
    """The address an employer sees, and which candidate it belongs to.

    Sub-addressed off one mailbox we own (``anna+<token>@goforay.io``): the
    token is derived by ``security.alias_token`` so an address can be verified
    without a lookup, and this row exists for the reverse direction and for
    revocation. Issuing an alias makes us the addressee of record on every
    application it is used for -- the deliberate trade in
    ``applications/alias.py``, not an accident.
    """

    __tablename__ = "candidate_aliases"
    __table_args__ = (
        UniqueConstraint("address", name="uq_candidate_alias_address"),
        Index("ix_candidate_aliases_candidate", "org_id", "candidate_id"),
        # Inbound mail arrives carrying a token, not an id: this is the lookup
        # on the hot path of every message the alias mailbox receives.
        Index("ix_candidate_aliases_local_part", "local_part"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("alias"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    # The full address, and the token we generated its local part from.
    address: Mapped[str] = mapped_column(String(255))
    local_part: Mapped[str] = mapped_column(String(120), default="")
    # active | revoked
    status: Mapped[str] = mapped_column(String(16), default="active")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AtsAccount(Base, TimestampMixin):
    """A candidate's login on one employer's ATS portal.

    Workday and Oracle make you register before you can apply, so without this
    every application to the same tenant creates another account and the
    verification mail arrives again. Username is the candidate's alias; the
    password is generated by us and encrypted at rest in ``token_blob`` by
    ``integrations/crypto.py`` -- the same treatment mailbox OAuth tokens get,
    for the same reason.
    """

    __tablename__ = "ats_accounts"
    __table_args__ = (
        UniqueConstraint("candidate_id", "vendor", "tenant", name="uq_ats_account_tenant"),
        Index("ix_ats_accounts_org_vendor", "org_id", "vendor"),
    )

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("atsacct")
    )
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    vendor: Mapped[str] = mapped_column(String(32))
    tenant: Mapped[str] = mapped_column(String(120), default="")
    username: Mapped[str] = mapped_column(String(255), default="")
    token_blob: Mapped[str] = mapped_column(Text, default="")
    # pending_verification | active | locked
    status: Mapped[str] = mapped_column(String(32), default="pending_verification")
    error: Mapped[str] = mapped_column(String(400), default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateAuthGrant(Base, TimestampMixin):
    """One single-use portal magic link, and whether it was spent.

    The signature alone is not enough: a texted link lands in SMS previews,
    browser history, and forwarded screenshots, so its JWT carries a ``jti``
    pointing here and the exchange endpoint marks the row consumed. A second
    exchange finds ``consumed_at`` set and refuses — replay is a 401, not a
    second session.
    """

    __tablename__ = "candidate_auth_grants"
    __table_args__ = (Index("ix_candidate_auth_grants_candidate", "org_id", "candidate_id"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cgrant"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    # sms | email | otp | recruiter -- how the link (or session) was issued.
    channel: Mapped[str] = mapped_column(String(16), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PortalOtp(Base, TimestampMixin):
    """A six-digit email code: the bookmark-return path into the portal.

    The code is HMAC-hashed at rest (a leaked table must not be a login), a
    verify attempt increments ``attempts`` and hard-fails at the cap, and
    issuance is capped per address per hour in the request endpoint. The
    request endpoint always answers 202 — an attacker learns nothing about
    which addresses exist.
    """

    __tablename__ = "portal_otps"
    __table_args__ = (Index("ix_portal_otps_email", "email"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("potp"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(255))
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------
# derived columns, guaranteed at the ORM layer
# --------------------------------------------------------------------------
#
# current_company_normalized / company_normalized are what the warm-contact
# SQL prefilter matches on, and they must hold for EVERY writer -- the enrich
# upsert, a test adding an Experience by hand, a future importer. A derived
# column maintained only by one call site is a column that silently goes
# stale, so the stamp lives on flush. normalize_company is imported lazily to
# keep models.py free of business-module imports at load time.


@event.listens_for(Experience, "before_insert", propagate=True)
@event.listens_for(Experience, "before_update", propagate=True)
def _stamp_experience_company_normalized(mapper, connection, target) -> None:
    from .enrich.taxonomy import normalize_company

    target.company_normalized = normalize_company(target.company or "")


@event.listens_for(Candidate, "before_insert", propagate=True)
@event.listens_for(Candidate, "before_update", propagate=True)
def _stamp_candidate_company_normalized(mapper, connection, target) -> None:
    from .enrich.taxonomy import normalize_company

    target.current_company_normalized = normalize_company(target.current_company or "")
