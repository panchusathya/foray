"""jobs queue, suppressions, send idempotency, org postal address

Revision ID: 9f2e4c8b1d70
Revises: 7c4b2a19f0e3
Create Date: 2026-08-18 06:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f2e4c8b1d70"
down_revision: str | None = "7c4b2a19f0e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column("postal_address", sa.String(length=400), server_default="", nullable=False),
    )

    op.create_table(
        "suppressions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_message_id", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "email", name="uq_suppression_org_email"),
    )
    op.create_index(op.f("ix_suppressions_org_id"), "suppressions", ["org_id"], unique=False)

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_job_org_idempotency"),
    )
    op.create_index(op.f("ix_jobs_org_id"), "jobs", ["org_id"], unique=False)
    op.create_index(op.f("ix_jobs_kind"), "jobs", ["kind"], unique=False)
    op.create_index(op.f("ix_jobs_run_at"), "jobs", ["run_at"], unique=False)
    op.create_index("ix_jobs_claim", "jobs", ["status", "run_at"], unique=False)

    op.create_index(
        "uq_messages_enrollment_step_outbound",
        "messages",
        ["enrollment_id", "step_id"],
        unique=True,
        sqlite_where=sa.text("direction = 'outbound' AND step_id IS NOT NULL"),
        postgresql_where=sa.text("direction = 'outbound' AND step_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_messages_enrollment_step_outbound", table_name="messages")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_index(op.f("ix_jobs_run_at"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_kind"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_org_id"), table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_suppressions_org_id"), table_name="suppressions")
    op.drop_table("suppressions")
    op.drop_column("orgs", "postal_address")
