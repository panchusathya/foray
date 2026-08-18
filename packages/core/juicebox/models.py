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


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("proj"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    role_title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(200), default="")
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
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("msg"))
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    enrollment_id: Mapped[str] = mapped_column(
        ForeignKey("enrollments.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str | None] = mapped_column(
        ForeignKey("sequence_steps.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(32), default="email")
    direction: Mapped[str] = mapped_column(String(16), default="outbound")
    to_address: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # queued|sent|failed|bounced|received
    error: Mapped[str] = mapped_column(Text, default="")
    provider_message_id: Mapped[str] = mapped_column(String(200), default="")

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    click_count: Mapped[int] = mapped_column(Integer, default=0)

    reply_intent: Mapped[str] = mapped_column(String(40), default="")
    reply_text: Mapped[str] = mapped_column(Text, default="")

    enrollment: Mapped[Enrollment] = relationship()
    candidate: Mapped[Candidate] = relationship()


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
