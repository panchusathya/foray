"""One person vouching for another, and what it earned them.

Backs the iMessage group-chat referral: a candidate puts our line and a friend
in a group, we answer once and leave, and every other active handle in that
chat becomes a pending row here. It confirms when the friend texts us
themselves -- we never open that conversation, so their own first message is
both the confirmation and the consent.

The unique index on (org_id, referee_address) is the anti-farming control, and
it is a database constraint rather than a check in the handler on purpose:
adding the same number to ten groups must be worth one referral even when a
future call site forgets to ask. It also settles two people racing to refer the
same friend, first writer wins.

``referee_address`` is 255, not 64. An iMessage handle is not always a phone
number -- Apple ID email addresses arrive too, the same reason
``intake_threads.from_address`` widened -- and a truncated handle is a referral
that can never be matched to the person who earns it.

``bonus_applications`` is stamped per row rather than read from settings at
spend time, so raising REFERRAL_BONUS_APPLICATIONS later cannot restate what an
old referral promised.

Revision ID: f3c9a2e64d17
Revises: e2f8b4a17c93
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3c9a2e64d17"
down_revision: str | None = "e2f8b4a17c93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "referrals",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SET NULL, not CASCADE: deleting a candidate should not erase the
        # record that somebody was referred, only who referred them.
        sa.Column(
            "referrer_candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("referrer_address", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("referee_address", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("group_chat_id", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "group_thread_id",
            sa.String(length=40),
            sa.ForeignKey("intake_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "referee_thread_id",
            sa.String(length=40),
            sa.ForeignKey("intake_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column(
            "bonus_applications", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # Inline, not a later create_unique_constraint: SQLite cannot ALTER a
        # constraint in, and this one is the anti-farming control -- it must
        # exist from the moment the table does, on both backends.
        sa.UniqueConstraint("org_id", "referee_address", name="uq_referral_referee"),
    )
    op.create_index("ix_referrals_org_id", "referrals", ["org_id"])
    # How a friend's first inbound finds the referral waiting for it.
    op.create_index(
        "ix_referrals_pending", "referrals", ["org_id", "status", "referee_address"]
    )
    # What earned_bonus sums.
    op.create_index(
        "ix_referrals_referrer", "referrals", ["org_id", "referrer_candidate_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_referrals_referrer", table_name="referrals")
    op.drop_index("ix_referrals_pending", table_name="referrals")
    op.drop_index("ix_referrals_org_id", table_name="referrals")
    # uq_referral_referee goes with the table.
    op.drop_table("referrals")
