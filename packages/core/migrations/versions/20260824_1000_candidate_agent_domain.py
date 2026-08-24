"""Candidate agent domain: proposals, applications, documents.

CANDIDATE_AGENT_PLAN §10's three new tables, plus the two normalized-company
columns that let warm-contact lookups filter in SQL instead of loading every
candidate in the org.

* ``job_proposals`` -- one posting shown to one candidate; consent-to-pursue.
  Deliberately not a status on ``job_matches``: that field is recruiter
  triage, and collapsing the axes makes a recruiter's shortlist read as the
  candidate agreeing to it.
* ``applications`` -- the pursuit. Unique per (candidate, posting): a
  duplicate submission reads as spam in an ATS. The consent columns exist so
  ``state.py`` can refuse ``submitted`` without a recorded per-application yes.
* ``candidate_documents`` -- file metadata; bytes live in the local document
  store. Parsed facts land in ``structured`` first and are promoted to the
  Candidate explicitly, never by blind upsert.
* ``candidates.current_company_normalized`` / ``experiences.company_normalized``
  -- backfilled with the same ``normalize_company`` the runtime writes with.

Revision ID: c7d2f83a91e4
Revises: a4c9e17f52b3
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from juicebox.enrich.taxonomy import normalize_company

revision: str = "c7d2f83a91e4"
down_revision: str | None = "a4c9e17f52b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 1000


def upgrade() -> None:
    op.create_table(
        "job_proposals",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_posting_id",
            sa.String(length=40),
            sa.ForeignKey("job_postings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_match_id",
            sa.String(length=40),
            sa.ForeignKey("job_matches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel", sa.String(length=24), server_default="", nullable=False),
        sa.Column(
            "thread_id",
            sa.String(length=40),
            sa.ForeignKey("intake_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            sa.String(length=40),
            sa.ForeignKey("intake_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=24), server_default="proposed", nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "responded_message_id",
            sa.String(length=40),
            sa.ForeignKey("intake_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decline_reason", sa.String(length=240), server_default="", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "candidate_id", "job_posting_id", name="uq_job_proposal_candidate_posting"
        ),
    )
    op.create_index("ix_job_proposals_org_id", "job_proposals", ["org_id"])
    op.create_index("ix_job_proposals_candidate_id", "job_proposals", ["candidate_id"])
    op.create_index("ix_job_proposals_job_posting_id", "job_proposals", ["job_posting_id"])
    op.create_index("ix_job_proposals_org_candidate", "job_proposals", ["org_id", "candidate_id"])
    op.create_index("ix_job_proposals_org_status", "job_proposals", ["org_id", "status"])

    op.create_table(
        "applications",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_posting_id",
            sa.String(length=40),
            sa.ForeignKey("job_postings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_match_id",
            sa.String(length=40),
            sa.ForeignKey("job_matches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "proposal_id",
            sa.String(length=40),
            sa.ForeignKey("job_proposals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            sa.String(length=40),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(length=24), server_default="candidate", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="draft", nullable=False),
        sa.Column("status_reason", sa.String(length=240), server_default="", nullable=False),
        sa.Column(
            "consenting_message_id",
            sa.String(length=40),
            sa.ForeignKey("intake_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_verbatim", sa.String(length=400), server_default="", nullable=False),
        sa.Column("submission_url", sa.Text(), server_default="", nullable=False),
        sa.Column("submission_method", sa.String(length=32), server_default="", nullable=False),
        sa.Column("submitted_by", sa.String(length=16), server_default="", nullable=False),
        sa.Column("submitted_by_user_id", sa.String(length=40), nullable=True),
        sa.Column("form_answers", sa.JSON(), nullable=False),
        sa.Column("networking", sa.JSON(), nullable=False),
        sa.Column("package_error", sa.String(length=400), server_default="", nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "candidate_id", "job_posting_id", name="uq_application_candidate_posting"
        ),
    )
    op.create_index("ix_applications_org_id", "applications", ["org_id"])
    op.create_index("ix_applications_candidate_id", "applications", ["candidate_id"])
    op.create_index("ix_applications_job_posting_id", "applications", ["job_posting_id"])
    op.create_index("ix_applications_org_status", "applications", ["org_id", "status"])
    op.create_index("ix_applications_org_candidate", "applications", ["org_id", "candidate_id"])

    op.create_table(
        "candidate_documents",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("applications.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), server_default="other", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("filename", sa.String(length=255), server_default="", nullable=False),
        sa.Column(
            "content_type",
            sa.String(length=100),
            server_default="application/pdf",
            nullable=False,
        ),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sha256", sa.String(length=64), server_default="", nullable=False),
        sa.Column("storage_path", sa.String(length=400), server_default="", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="ready", nullable=False),
        sa.Column("text_extracted", sa.Text(), server_default="", nullable=False),
        sa.Column("structured", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), server_default="", nullable=False),
        sa.Column("generated_by", sa.String(length=20), server_default="", nullable=False),
        sa.Column("source", sa.String(length=32), server_default="", nullable=False),
        sa.Column(
            "thread_id",
            sa.String(length=40),
            sa.ForeignKey("intake_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error", sa.String(length=400), server_default="", nullable=False),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_candidate_documents_org_id", "candidate_documents", ["org_id"])
    op.create_index(
        "ix_candidate_documents_candidate_id", "candidate_documents", ["candidate_id"]
    )
    op.create_index(
        "ix_candidate_documents_candidate", "candidate_documents", ["org_id", "candidate_id"]
    )
    op.create_index(
        "ix_candidate_documents_application", "candidate_documents", ["application_id"]
    )

    op.add_column(
        "candidates",
        sa.Column(
            "current_company_normalized", sa.String(length=240), server_default="", nullable=False
        ),
    )
    op.create_index(
        "ix_candidates_current_company_normalized", "candidates", ["current_company_normalized"]
    )
    op.add_column(
        "experiences",
        sa.Column("company_normalized", sa.String(length=240), server_default="", nullable=False),
    )
    op.create_index("ix_experiences_company_normalized", "experiences", ["company_normalized"])

    _backfill_normalized()


def _backfill_normalized() -> None:
    """Fill the normalized columns with the function the runtime writes with.

    Imports app code on purpose: two spellings of the normalizer drifting
    apart would make the backfilled rows unfindable by the queries that read
    the runtime-written ones.
    """
    bind = op.get_bind()

    candidates = sa.table(
        "candidates", sa.column("id", sa.String), sa.column("current_company", sa.String)
    )
    rows = bind.execute(sa.select(candidates.c.id, candidates.c.current_company)).fetchall()
    pending = [
        {"id": row_id, "norm": normalize_company(company or "")}
        for row_id, company in rows
        if company
    ]
    for start in range(0, len(pending), _BATCH):
        bind.execute(
            sa.text("UPDATE candidates SET current_company_normalized = :norm WHERE id = :id"),
            pending[start : start + _BATCH],
        )

    experiences = sa.table(
        "experiences", sa.column("id", sa.String), sa.column("company", sa.String)
    )
    rows = bind.execute(sa.select(experiences.c.id, experiences.c.company)).fetchall()
    pending = [
        {"id": row_id, "norm": normalize_company(company or "")}
        for row_id, company in rows
        if company
    ]
    for start in range(0, len(pending), _BATCH):
        bind.execute(
            sa.text("UPDATE experiences SET company_normalized = :norm WHERE id = :id"),
            pending[start : start + _BATCH],
        )


def downgrade() -> None:
    op.drop_index("ix_experiences_company_normalized", table_name="experiences")
    op.drop_column("experiences", "company_normalized")
    op.drop_index("ix_candidates_current_company_normalized", table_name="candidates")
    op.drop_column("candidates", "current_company_normalized")

    op.drop_index("ix_candidate_documents_application", table_name="candidate_documents")
    op.drop_index("ix_candidate_documents_candidate", table_name="candidate_documents")
    op.drop_index("ix_candidate_documents_candidate_id", table_name="candidate_documents")
    op.drop_index("ix_candidate_documents_org_id", table_name="candidate_documents")
    op.drop_table("candidate_documents")

    op.drop_index("ix_applications_org_candidate", table_name="applications")
    op.drop_index("ix_applications_org_status", table_name="applications")
    op.drop_index("ix_applications_job_posting_id", table_name="applications")
    op.drop_index("ix_applications_candidate_id", table_name="applications")
    op.drop_index("ix_applications_org_id", table_name="applications")
    op.drop_table("applications")

    op.drop_index("ix_job_proposals_org_status", table_name="job_proposals")
    op.drop_index("ix_job_proposals_org_candidate", table_name="job_proposals")
    op.drop_index("ix_job_proposals_job_posting_id", table_name="job_proposals")
    op.drop_index("ix_job_proposals_candidate_id", table_name="job_proposals")
    op.drop_index("ix_job_proposals_org_id", table_name="job_proposals")
    op.drop_table("job_proposals")
