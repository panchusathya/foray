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
    text,
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
    """Org-level do-not-contact list. Emails are stored lowercased."""

    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_suppression_org_email"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sup"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255))
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
