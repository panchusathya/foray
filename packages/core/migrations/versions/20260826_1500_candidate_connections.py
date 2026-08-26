"""Candidate app connections (Gmail / Outlook / GitHub).

One table: the encrypted OAuth grant a candidate made from a text thread, and
the outcome of its one targeted index. Anchored on the thread because the
thread predates the candidate — the same reasoning ``candidate_documents`` and
``consent_records`` already encode. Deliberately not a ``mailbox_connections``
row: that table is a recruiter's send-capable, continuously synced mailbox;
this is a read-only grant no sync loop touches.

``index_state`` carries status, counts and ids only. There is no subject
column and no body column, and there must never be one — the discovery
sweep's rule, applied to the candidate side.

Revision ID: a9d47c31e8b5
Revises: f7c31a9e2d48
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9d47c31e8b5"
down_revision: str | None = "f7c31a9e2d48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate_connections",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            sa.String(length=40),
            sa.ForeignKey("intake_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "consent_id",
            sa.String(length=40),
            sa.ForeignKey("consent_records.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(length=40), server_default="", nullable=False),
        sa.Column("address", sa.String(length=255), server_default="", nullable=False),
        sa.Column("display_name", sa.String(length=200), server_default="", nullable=False),
        sa.Column("provider_account_id", sa.String(length=120), server_default="", nullable=False),
        sa.Column("scopes", sa.String(length=400), server_default="", nullable=False),
        sa.Column("token_blob", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("index_state", sa.JSON(), nullable=False),
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "thread_id", "provider", name="uq_candidate_connection_thread_provider"
        ),
    )
    op.create_index("ix_candidate_connections_org", "candidate_connections", ["org_id"])
    op.create_index("ix_candidate_connections_org_id", "candidate_connections", ["org_id"])
    op.create_index("ix_candidate_connections_thread_id", "candidate_connections", ["thread_id"])


def downgrade() -> None:
    op.drop_index("ix_candidate_connections_thread_id", table_name="candidate_connections")
    op.drop_index("ix_candidate_connections_org_id", table_name="candidate_connections")
    op.drop_index("ix_candidate_connections_org", table_name="candidate_connections")
    op.drop_table("candidate_connections")
