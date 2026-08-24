"""Messaging governance: send-window timezone, message origin, consent records.

CANDIDATE_AGENT_PLAN §2-§4, the parts that need schema:

* ``intake_threads.timezone`` / ``timezone_source`` -- the resolved send-window
  zone and how it was learned. Stored rather than recomputed so the window a
  send ran under is auditable; empty means the conservative unknown-zone
  window was in force, which is the honest state for every existing row.
* ``intake_messages.origin`` -- reply | proactive | system. Nudge and
  proposal budgets count proactive rows instead of a counter column, so the
  budget cannot drift from what was actually sent. Every existing row was an
  answer to something inbound, so ``reply`` is the true backfill, not just
  the safe one.
* ``consent_records`` -- consent provenance, append-only. With sourced
  outbound in scope the artifact is the defence: channel, scope, source, the
  exact disclosure shown and its version, and revocation as its own fact.

Revision ID: a4c9e17f52b3
Revises: e5f0a92c7d18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4c9e17f52b3"
down_revision: str | None = "e5f0a92c7d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intake_threads",
        sa.Column("timezone", sa.String(length=64), server_default="", nullable=False),
    )
    op.add_column(
        "intake_threads",
        sa.Column("timezone_source", sa.String(length=24), server_default="", nullable=False),
    )
    op.add_column(
        "intake_messages",
        sa.Column("origin", sa.String(length=16), server_default="reply", nullable=False),
    )

    op.create_table(
        "consent_records",
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
            sa.ForeignKey("candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "thread_id",
            sa.String(length=40),
            sa.ForeignKey("intake_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "call_id",
            sa.String(length=40),
            sa.ForeignKey("intake_calls.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel", sa.String(length=24), server_default="", nullable=False),
        sa.Column("scope", sa.String(length=32), server_default="messaging", nullable=False),
        sa.Column("source", sa.String(length=32), server_default="", nullable=False),
        sa.Column("disclosure_text", sa.Text(), server_default="", nullable=False),
        sa.Column("disclosure_version", sa.String(length=40), server_default="", nullable=False),
        sa.Column("verbatim", sa.String(length=400), server_default="", nullable=False),
        sa.Column("granted", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor_id", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_consent_records_org_id", "consent_records", ["org_id"])
    op.create_index(
        "ix_consent_records_org_candidate", "consent_records", ["org_id", "candidate_id"]
    )
    op.create_index("ix_consent_records_thread", "consent_records", ["thread_id"])


def downgrade() -> None:
    op.drop_index("ix_consent_records_thread", table_name="consent_records")
    op.drop_index("ix_consent_records_org_candidate", table_name="consent_records")
    op.drop_index("ix_consent_records_org_id", table_name="consent_records")
    op.drop_table("consent_records")
    op.drop_column("intake_messages", "origin")
    op.drop_column("intake_threads", "timezone_source")
    op.drop_column("intake_threads", "timezone")
