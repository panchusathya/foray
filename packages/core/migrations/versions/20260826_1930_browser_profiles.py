"""Warm apply-browser profiles, and the ATS-account handover stamp.

``browser_profiles`` keeps what makes a browser look returning instead of
minted-this-second: encrypted Playwright storage_state, the sticky proxy
session token (one candidate, one egress IP), and the UA the profile was born
with. Dark by default -- ``APPLY_BROWSER_PROFILES=false`` leaves the apply
browser byte-for-byte its proven cold-context self, and the CDP Scraping
Browser path ignores profiles entirely (it owns its own fingerprint).

``ats_accounts.handed_over_at``: the candidate took the account over through
the board's own password reset, so the credential we generated stops being
offered to submit runs and whatever password they set is theirs -- never seen,
never stored.

Revision ID: f4c8a29d71e3
Revises: a7d3f92c48e1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4c8a29d71e3"
down_revision: str | None = "a7d3f92c48e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_profiles",
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
        sa.Column("storage_state", sa.Text(), nullable=False, server_default=""),
        sa.Column("egress_session", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("user_agent", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "candidate_id", name="uq_browser_profile_candidate"),
    )
    op.add_column(
        "ats_accounts",
        sa.Column("handed_over_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ats_accounts", "handed_over_at")
    op.drop_table("browser_profiles")
