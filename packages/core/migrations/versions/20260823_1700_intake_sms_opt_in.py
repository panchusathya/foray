"""SMS opt-in on the intake call: the TCPA consent record.

One boolean, latched exactly like ``consent_recording``: the agent asks the
opt-in question at wrap-up, the platform's post-call analysis reads the answer
back out of the transcript, and the webhook handler only ever sets it true.
Un-consenting happens by the person replying STOP to a message, never by a
webhook omitting a field.

No verbatim-language column, deliberately. The evidence a TCPA claim needs --
what was asked, what was answered, and when -- is the transcript already stored
on this same row, with the call's own timestamps. A copy of the phrasing would
be a second source of truth that can drift from the first.

Server default false, and no backfill: every call recorded before this column
existed was never asked the opt-in question, so false is not just the safe
default, it is the true one.

Revision ID: e5f0a92c7d18
Revises: d2b8c31f9e47
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f0a92c7d18"
down_revision: str | None = "b7e2c94af013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intake_calls",
        sa.Column("sms_opt_in", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("intake_calls", "sms_opt_in")
