"""Numbers we own, and the org each one belongs to.

This table is the answer to "whose call is this?". A candidate on a phone call
has no principal, so the org cannot come from the request -- and it must not come
from the webhook body, which the caller can influence and which would make a
cross-tenant write out of a forged payload. The number that was dialled is the
one thing in an inbound call that the caller does not choose.

``phone`` is unique **globally**, not per org. A real-world number belongs to
exactly one org; scoping the constraint per org would let two orgs claim the same
number and race for its calls, which is the failure this table exists to prevent.

Web calls have no dialled number and resolve by ``call_id`` against the
``intake_calls`` row written when the call was placed -- the same shape as
``routers/track.py`` reading ``message.org_id`` off a row instead of a query
string.

No backfill. The number is registered by an operator once the agent exists, and
inventing a mapping here would be guessing which org owns a phone line.

Revision ID: d2b8c31f9e47
Revises: c1a7f4e05b93
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2b8c31f9e47"
down_revision: str | None = "c1a7f4e05b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intake_numbers",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=32), server_default="retell", nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=120), server_default="", nullable=False),
        sa.Column("inbound_agent_id", sa.String(length=120), server_default="", nullable=False),
        sa.Column("outbound_agent_id", sa.String(length=120), server_default="", nullable=False),
        # Deactivating a number must not delete the row: every past IntakeCall
        # resolved its org through here, and removing it makes that history
        # unattributable.
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("phone", name="uq_intake_number_phone"),
    )
    op.create_index("ix_intake_numbers_org_id", "intake_numbers", ["org_id"])
    op.create_index("ix_intake_numbers_org_active", "intake_numbers", ["org_id", "active"])


def downgrade() -> None:
    op.drop_index("ix_intake_numbers_org_active", table_name="intake_numbers")
    op.drop_index("ix_intake_numbers_org_id", table_name="intake_numbers")
    op.drop_table("intake_numbers")
