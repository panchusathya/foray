"""Per-call field provenance, and a phone column on suppressions.

``intake_field_answers`` is audit, not a second source of truth — jobmatch
still reads ``candidate_preferences`` only. ``asked=false`` is a core-mode
trim, not a missed question.

Phone suppression: a STOP from an unknown number has no email. ``email``
becomes nullable so two empty-email rows do not collide on
``uq_suppression_org_email``. A partial unique on ``(org_id, phone)`` covers
the phone-only case.

Revision ID: b4d2e80c17a9
Revises: e7c4a19b2d80
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d2e80c17a9"
down_revision: str | None = "e7c4a19b2d80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intake_field_answers",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "intake_call_id",
            sa.String(length=40),
            sa.ForeignKey("intake_calls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("field_key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("verbatim", sa.Text(), server_default="", nullable=False),
        sa.Column("confidence", sa.String(length=16), server_default="", nullable=False),
        sa.Column("declined", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("asked", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("intake_call_id", "field_key", name="uq_intake_field_answer_call_key"),
    )
    op.create_index(
        "ix_intake_field_answers_org_call", "intake_field_answers", ["org_id", "intake_call_id"]
    )
    op.create_index(
        "ix_intake_field_answers_candidate", "intake_field_answers", ["org_id", "candidate_id"]
    )
    op.create_index("ix_intake_field_answers_org_id", "intake_field_answers", ["org_id"])
    op.create_index(
        "ix_intake_field_answers_intake_call_id", "intake_field_answers", ["intake_call_id"]
    )
    op.create_index(
        "ix_intake_field_answers_candidate_id", "intake_field_answers", ["candidate_id"]
    )

    with op.batch_alter_table("suppressions") as batch:
        batch.alter_column("email", existing_type=sa.String(length=255), nullable=True)
        batch.add_column(sa.Column("phone", sa.String(length=32), server_default="", nullable=False))

    op.create_index(
        "uq_suppression_org_phone",
        "suppressions",
        ["org_id", "phone"],
        unique=True,
        sqlite_where=sa.text("phone != ''"),
        postgresql_where=sa.text("phone != ''"),
    )


def downgrade() -> None:
    op.drop_index("uq_suppression_org_phone", table_name="suppressions")
    with op.batch_alter_table("suppressions") as batch:
        batch.drop_column("phone")
        batch.alter_column("email", existing_type=sa.String(length=255), nullable=False)
    op.drop_index("ix_intake_field_answers_candidate_id", table_name="intake_field_answers")
    op.drop_index("ix_intake_field_answers_intake_call_id", table_name="intake_field_answers")
    op.drop_index("ix_intake_field_answers_org_id", table_name="intake_field_answers")
    op.drop_index("ix_intake_field_answers_candidate", table_name="intake_field_answers")
    op.drop_index("ix_intake_field_answers_org_call", table_name="intake_field_answers")
    op.drop_table("intake_field_answers")
