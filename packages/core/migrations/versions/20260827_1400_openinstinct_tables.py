"""The two OpenInstinct tables, which shipped as models with no migration.

``OpenInstinctIdentityLink`` and ``OpenInstinctApplicationTask`` landed with the
OpenInstinct integration but nothing created their tables, so
``test_migrated_schema_matches_the_models`` and ``test_every_model_table_exists``
have been red on main since. Unlike the ``candidate_preference_facts`` index
follow-up, nothing partial ran in prod here -- the tables do not exist at all --
so this creates them outright rather than patching a half-built shape.

Both single-column ``index=True`` and the named ``__table_args__`` indexes are
written out, because the drift check compares against every index the model
declares. That is the exact trap ``d7e4b2a1c9f6`` had to clean up afterwards:
declaring the columns is not declaring their indexes.

``ix_openinstinct_link_candidate`` duplicates the ``candidate_id`` index that
``index=True`` already creates. It is reproduced anyway -- the model declares
both, and the drift check compares names, so dropping the redundant one here
would simply move the failure rather than fix it.

Revision ID: c4a91f7be250
Revises: e8b3c1d4a7f2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a91f7be250"
down_revision: str | Sequence[str] | None = "e8b3c1d4a7f2"
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
            index=True,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("external_user_id", sa.String(length=160), nullable=False),
        sa.Column("verified_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("verified_phone", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("external_user_id", name="uq_openinstinct_external_user"),
        sa.UniqueConstraint("org_id", "candidate_id", name="uq_openinstinct_org_candidate"),
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
            index=True,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("external_task_id", sa.String(length=160), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="package_pending"
        ),
        sa.Column("conversation_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "external_task_id", name="uq_openinstinct_task_external"),
        sa.UniqueConstraint("application_id", name="uq_openinstinct_task_application"),
    )
    op.create_index(
        "ix_openinstinct_task_candidate",
        "openinstinct_application_tasks",
        ["org_id", "candidate_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_openinstinct_task_candidate", table_name="openinstinct_application_tasks")
    op.drop_table("openinstinct_application_tasks")
    op.drop_index("ix_openinstinct_link_candidate", table_name="openinstinct_identity_links")
    op.drop_table("openinstinct_identity_links")
