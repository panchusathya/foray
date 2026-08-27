"""Candidate fact provenance, protected resumes, and vault revocation.

Revision ID: b8e12a4c7f1d
Revises: f4c8a29d71e3
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e12a4c7f1d"
down_revision: str | None = "f4c8a29d71e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("candidate_documents", sa.Column("protected_default", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("ats_accounts", sa.Column("credential_origin", sa.String(length=32), nullable=False, server_default="generated"))
    op.add_column("ats_accounts", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    # An old handover contains a password Juicebox no longer owns.  Treat it as
    # a revoked credential rather than accidentally attempting to use it.
    op.execute("UPDATE ats_accounts SET status = 'needs_reentry', credential_origin = 'legacy_handover', token_blob = '', revoked_at = handed_over_at WHERE handed_over_at IS NOT NULL")
    op.create_table(
        "candidate_preference_facts",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("org_id", sa.String(length=40), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_id", sa.String(length=40), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_preference_id", sa.String(length=40), sa.ForeignKey("candidate_preferences.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field_key", sa.String(length=64), nullable=False),
        sa.Column("source_call_id", sa.String(length=40), sa.ForeignKey("intake_calls.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_thread_id", sa.String(length=40), sa.ForeignKey("intake_threads.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_message_id", sa.String(length=40), sa.ForeignKey("intake_messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("value_revision", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("direct_candidate_statement", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("declined", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_preference_id", "field_key", name="uq_candidate_preference_fact"),
    )
    op.create_index("ix_candidate_preference_facts_candidate", "candidate_preference_facts", ["candidate_id", "field_key"])


def downgrade() -> None:
    op.drop_index("ix_candidate_preference_facts_candidate", table_name="candidate_preference_facts")
    op.drop_table("candidate_preference_facts")
    op.drop_column("ats_accounts", "revoked_at")
    op.drop_column("ats_accounts", "credential_origin")
    op.drop_column("candidate_documents", "protected_default")
