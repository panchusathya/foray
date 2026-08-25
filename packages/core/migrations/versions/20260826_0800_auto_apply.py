"""Auto-apply: submission attempts, candidate aliases, ATS portal accounts.

Phase F of CANDIDATE_AGENT_PLAN. Three tables and five columns on
``job_postings``:

* ``application_submissions`` -- one row per attempt to actually send an
  application. Separate from the application because an attempt can park on an
  email verification, fail on a changed form, and succeed on the retry, and all
  three are facts worth keeping.
* ``candidate_aliases`` -- the address an employer sees, sub-addressed off one
  mailbox we own.
* ``ats_accounts`` -- the portal login Workday and Oracle make you create
  before you can apply, so the second role at the same employer reuses it.

The ``job_postings`` columns cache which ATS is behind a posting.
``Application.submitting_at`` stamps the in-flight state; the ``submitting``
status itself needs no migration -- ``applications.status`` is a plain
``String(32)`` with no CHECK constraint, and the vocabulary is validated in
``models.py`` like every other status in this schema.

Revision ID: c4e81b7f3a26
Revises: e5a1c73f4b28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e81b7f3a26"
down_revision: str | None = "e5a1c73f4b28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_postings",
        sa.Column("ats_vendor", sa.String(length=32), server_default="", nullable=False),
    )
    op.add_column(
        "job_postings",
        sa.Column("ats_tenant", sa.String(length=120), server_default="", nullable=False),
    )
    op.add_column(
        "job_postings",
        sa.Column("ats_external_job_id", sa.String(length=120), server_default="", nullable=False),
    )
    op.add_column(
        "job_postings", sa.Column("apply_url", sa.Text(), server_default="", nullable=False)
    )
    op.add_column(
        "job_postings", sa.Column("ats_resolved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_job_postings_ats_vendor", "job_postings", ["org_id", "ats_vendor"])

    op.add_column(
        "applications", sa.Column("submitting_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "application_submissions",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("vendor", sa.String(length=32), server_default="", nullable=False),
        sa.Column("method", sa.String(length=32), server_default="", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("external_id", sa.String(length=200), server_default="", nullable=False),
        sa.Column("confirmation_ref", sa.String(length=400), server_default="", nullable=False),
        sa.Column("error", sa.String(length=600), server_default="", nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("verification", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("application_id", "attempt", name="uq_application_submission_attempt"),
    )
    op.create_index("ix_application_submissions_org_id", "application_submissions", ["org_id"])
    op.create_index(
        "ix_application_submissions_application_id", "application_submissions", ["application_id"]
    )
    op.create_index(
        "ix_application_submissions_application", "application_submissions", ["application_id"]
    )
    op.create_index(
        "ix_application_submissions_org_status", "application_submissions", ["org_id", "status"]
    )
    # The double-submit guard, at the level that survives two workers racing.
    # Partial indexes are Postgres and SQLite only, which is both of ours.
    op.create_index(
        "uq_application_submission_succeeded",
        "application_submissions",
        ["application_id"],
        unique=True,
        sqlite_where=sa.text("status = 'succeeded'"),
        postgresql_where=sa.text("status = 'succeeded'"),
    )

    op.create_table(
        "candidate_aliases",
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
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("local_part", sa.String(length=120), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("address", name="uq_candidate_alias_address"),
    )
    op.create_index("ix_candidate_aliases_org_id", "candidate_aliases", ["org_id"])
    op.create_index("ix_candidate_aliases_candidate_id", "candidate_aliases", ["candidate_id"])
    op.create_index(
        "ix_candidate_aliases_candidate", "candidate_aliases", ["org_id", "candidate_id"]
    )
    # Inbound mail arrives with a token, not an id: this is the lookup on the
    # hot path of every message the alias mailbox receives.
    op.create_index("ix_candidate_aliases_local_part", "candidate_aliases", ["local_part"])

    op.create_table(
        "ats_accounts",
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
        sa.Column("vendor", sa.String(length=32), nullable=False),
        sa.Column("tenant", sa.String(length=120), server_default="", nullable=False),
        sa.Column("username", sa.String(length=255), server_default="", nullable=False),
        sa.Column("token_blob", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="pending_verification", nullable=False
        ),
        sa.Column("error", sa.String(length=400), server_default="", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_id", "vendor", "tenant", name="uq_ats_account_tenant"),
    )
    op.create_index("ix_ats_accounts_org_id", "ats_accounts", ["org_id"])
    op.create_index("ix_ats_accounts_candidate_id", "ats_accounts", ["candidate_id"])
    op.create_index("ix_ats_accounts_org_vendor", "ats_accounts", ["org_id", "vendor"])


def downgrade() -> None:
    op.drop_index("ix_ats_accounts_org_vendor", table_name="ats_accounts")
    op.drop_index("ix_ats_accounts_candidate_id", table_name="ats_accounts")
    op.drop_index("ix_ats_accounts_org_id", table_name="ats_accounts")
    op.drop_table("ats_accounts")

    op.drop_index("ix_candidate_aliases_local_part", table_name="candidate_aliases")
    op.drop_index("ix_candidate_aliases_candidate", table_name="candidate_aliases")
    op.drop_index("ix_candidate_aliases_candidate_id", table_name="candidate_aliases")
    op.drop_index("ix_candidate_aliases_org_id", table_name="candidate_aliases")
    op.drop_table("candidate_aliases")

    op.drop_index("uq_application_submission_succeeded", table_name="application_submissions")
    op.drop_index("ix_application_submissions_org_status", table_name="application_submissions")
    op.drop_index("ix_application_submissions_application", table_name="application_submissions")
    op.drop_index("ix_application_submissions_application_id", table_name="application_submissions")
    op.drop_index("ix_application_submissions_org_id", table_name="application_submissions")
    op.drop_table("application_submissions")

    op.drop_column("applications", "submitting_at")

    op.drop_index("ix_job_postings_ats_vendor", table_name="job_postings")
    op.drop_column("job_postings", "ats_resolved_at")
    op.drop_column("job_postings", "apply_url")
    op.drop_column("job_postings", "ats_external_job_id")
    op.drop_column("job_postings", "ats_tenant")
    op.drop_column("job_postings", "ats_vendor")
