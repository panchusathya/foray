"""Add the private OpenInstinct candidate bridge tables.

Revision ID: f4a8c2d9e761
Revises: e8b3c1d4a7f2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a8c2d9e761"
down_revision: str | None = "e8b3c1d4a7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "openinstinct_identity_links",
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
        sa.Column("external_user_id", sa.String(length=160), nullable=False),
        sa.Column("verified_email", sa.String(length=255), nullable=False),
        sa.Column("verified_phone", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("external_user_id", name="uq_openinstinct_external_user"),
        sa.UniqueConstraint("org_id", "candidate_id", name="uq_openinstinct_org_candidate"),
    )
    op.create_index(
        "ix_openinstinct_identity_links_org_id", "openinstinct_identity_links", ["org_id"]
    )
    op.create_index(
        "ix_openinstinct_identity_links_candidate_id",
        "openinstinct_identity_links",
        ["candidate_id"],
    )
    op.create_index(
        "ix_openinstinct_link_candidate", "openinstinct_identity_links", ["candidate_id"]
    )

    op.create_table(
        "openinstinct_application_tasks",
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
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_task_id", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("conversation_url", sa.String(length=500), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "external_task_id", name="uq_openinstinct_task_external"),
        sa.UniqueConstraint("application_id", name="uq_openinstinct_task_application"),
    )
    op.create_index(
        "ix_openinstinct_application_tasks_org_id", "openinstinct_application_tasks", ["org_id"]
    )
    op.create_index(
        "ix_openinstinct_application_tasks_candidate_id",
        "openinstinct_application_tasks",
        ["candidate_id"],
    )
    op.create_index(
        "ix_openinstinct_application_tasks_application_id",
        "openinstinct_application_tasks",
        ["application_id"],
    )
    op.create_index(
        "ix_openinstinct_task_candidate",
        "openinstinct_application_tasks",
        ["org_id", "candidate_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_table("openinstinct_application_tasks")
    op.drop_table("openinstinct_identity_links")
