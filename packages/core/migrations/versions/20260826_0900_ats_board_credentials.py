"""Employer board keys, and the parked-attempt window.

``ats_board_credentials`` holds an employer's own API key for their job board,
per (org, vendor, tenant). It replaces a global ``GREENHOUSE_API_KEY`` setting
that was the wrong shape: Greenhouse, Lever and Ashby all authenticate with a
key the employer issues from their own account, so one key per deployment could
never post to more than one board. Where we hold no key -- the open market --
applications go through the employer's hosted form in a browser instead, which
needs none.

Encrypted at rest in ``token_blob`` via ``integrations/crypto``, the same
treatment mailbox OAuth tokens and ``ats_accounts`` already get.

Revision ID: a91d3f70c5b8
Revises: c4e81b7f3a26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a91d3f70c5b8"
down_revision: str | None = "c4e81b7f3a26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ats_board_credentials",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vendor", sa.String(length=32), nullable=False),
        sa.Column("tenant", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=120), server_default="", nullable=False),
        sa.Column("token_blob", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "vendor", "tenant", name="uq_ats_board_credential"),
    )
    op.create_index("ix_ats_board_credentials_org_id", "ats_board_credentials", ["org_id"])
    op.create_index(
        "ix_ats_board_credentials_org_vendor", "ats_board_credentials", ["org_id", "vendor"]
    )


def downgrade() -> None:
    op.drop_index("ix_ats_board_credentials_org_vendor", table_name="ats_board_credentials")
    op.drop_index("ix_ats_board_credentials_org_id", table_name="ats_board_credentials")
    op.drop_table("ats_board_credentials")
