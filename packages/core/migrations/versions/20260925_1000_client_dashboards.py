"""client dashboards

A hiring company's own login onto the people we have shared with it: their
stage in the client's process and a note thread both sides read. Separate
tables from projects on purpose -- a project stage is our internal pipeline,
these are the client's decisions -- and a separate login table from ``users``
so a client token can never resolve to a recruiter.

Revision ID: 9b2d6e4a1c57
Revises: f3a7c1d94e28
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9b2d6e4a1c57"
down_revision = "f3a7c1d94e28"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "client_accounts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "name", name="uq_client_account_org_name"),
    )
    op.create_index("ix_client_accounts_org_id", "client_accounts", ["org_id"])

    op.create_table(
        "client_users",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("client_account_id", sa.String(length=40), nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["client_account_id"], ["client_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_client_users_org_id", "client_users", ["org_id"])
    op.create_index("ix_client_users_client_account_id", "client_users", ["client_account_id"])

    op.create_table(
        "client_submissions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("client_account_id", sa.String(length=40), nullable=False),
        sa.Column("candidate_id", sa.String(length=40), nullable=True),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("current_title", sa.String(length=240), nullable=False, server_default=""),
        sa.Column("current_company", sa.String(length=240), nullable=False, server_default=""),
        sa.Column("location", sa.String(length=240), nullable=False, server_default=""),
        sa.Column("linkedin_url", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("role_title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("stage", sa.String(length=32), nullable=False, server_default="awaiting"),
        sa.Column("stage_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "stage_changed_by_kind", sa.String(length=16), nullable=False, server_default=""
        ),
        sa.Column(
            "stage_changed_by_name", sa.String(length=200), nullable=False, server_default=""
        ),
        sa.Column("shared_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["client_account_id"], ["client_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_account_id", "dedupe_key", name="uq_client_submission_dedupe"
        ),
    )
    op.create_index("ix_client_submissions_org_id", "client_submissions", ["org_id"])
    op.create_index(
        "ix_client_submissions_client_account_id", "client_submissions", ["client_account_id"]
    )
    op.create_index("ix_client_submissions_candidate_id", "client_submissions", ["candidate_id"])

    op.create_table(
        "client_submission_notes",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("submission_id", sa.String(length=40), nullable=False),
        sa.Column("author_kind", sa.String(length=16), nullable=False),
        sa.Column("author_id", sa.String(length=40), nullable=True),
        sa.Column("author_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["client_submissions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_client_submission_notes_org_id", "client_submission_notes", ["org_id"])
    op.create_index(
        "ix_client_note_submission", "client_submission_notes", ["submission_id", "created_at"]
    )

    op.create_table(
        "client_submission_events",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("submission_id", sa.String(length=40), nullable=False),
        sa.Column("from_stage", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("to_stage", sa.String(length=32), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["client_submissions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_client_submission_events_org_id", "client_submission_events", ["org_id"]
    )
    op.create_index(
        "ix_client_event_submission", "client_submission_events", ["submission_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("client_submission_events")
    op.drop_table("client_submission_notes")
    op.drop_table("client_submissions")
    op.drop_table("client_users")
    op.drop_table("client_accounts")
