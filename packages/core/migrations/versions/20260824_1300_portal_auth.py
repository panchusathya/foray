"""Portal auth: single-use magic-link grants and email OTPs.

Two small tables behind apply.goforay.io's passwordless login. The grant row
is what makes a texted link single-use — the JWT's signature proves we minted
it, ``consumed_at`` proves nobody spent it first. The OTP row is the
bookmark-return path, hashed at rest and attempt-capped.

Revision ID: 8a5c31e7d2f9
Revises: f2b6d94c81a7
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8a5c31e7d2f9"
down_revision: str | None = "f2b6d94c81a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate_auth_grants",
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
        sa.Column("channel", sa.String(length=16), server_default="", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_candidate_auth_grants_org_id", "candidate_auth_grants", ["org_id"])
    op.create_index(
        "ix_candidate_auth_grants_candidate_id", "candidate_auth_grants", ["candidate_id"]
    )
    op.create_index(
        "ix_candidate_auth_grants_candidate",
        "candidate_auth_grants",
        ["org_id", "candidate_id"],
    )

    op.create_table(
        "portal_otps",
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
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_portal_otps_org_id", "portal_otps", ["org_id"])
    op.create_index("ix_portal_otps_candidate_id", "portal_otps", ["candidate_id"])
    op.create_index("ix_portal_otps_email", "portal_otps", ["email"])


def downgrade() -> None:
    op.drop_index("ix_portal_otps_email", table_name="portal_otps")
    op.drop_index("ix_portal_otps_candidate_id", table_name="portal_otps")
    op.drop_index("ix_portal_otps_org_id", table_name="portal_otps")
    op.drop_table("portal_otps")
    op.drop_index("ix_candidate_auth_grants_candidate", table_name="candidate_auth_grants")
    op.drop_index("ix_candidate_auth_grants_candidate_id", table_name="candidate_auth_grants")
    op.drop_index("ix_candidate_auth_grants_org_id", table_name="candidate_auth_grants")
    op.drop_table("candidate_auth_grants")
