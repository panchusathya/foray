"""Don't let the new per-mailbox cap cut an existing mailbox's throughput.

The rotation migration gave every mailbox `daily_send_cap = 40`, including the
ones already connected. That number is a sensible starting point for a *new*
mailbox on a cold domain, and a silent throughput cut for one already in use:
a deployment sending up to OUTREACH_DAILY_CAP (200 by default) from a single
established mailbox would suddenly defer everything past 40 a day, with no
error anywhere — the sends just sit queued.

So mailboxes that predate the pool inherit the limit they were actually running
under, and only mailboxes connected from now on start at 40. Same principle as
backfilling sender_mode='fixed': a deploy must not change how much mail an
existing setup sends, in either direction.

"Predates the pool" is read as "still carries the stamped 40", which is both
simpler than a timestamp cutoff and safer: a cap somebody has already tuned to
40 on purpose is indistinguishable from the stamp, but tuning it to anything
else is respected rather than overwritten.

Note the org cap below is a constant. A migration cannot read the running
process's env, and a schema change that behaves differently per deployment is
worse than one honest number — if a deployment ran a non-default
OUTREACH_DAILY_CAP, adjust the per-mailbox cap in Settings after deploying.

Revision ID: d1e4f70b93ac
Revises: c5d9a1f30e78
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d1e4f70b93ac"
down_revision: str | None = "c5d9a1f30e78"
branch_labels = None
depends_on = None

# The shipped default for a newly connected mailbox, and the value the previous
# migration stamped onto every existing row.
_NEW_MAILBOX_CAP = 40
# config.py's OUTREACH_DAILY_CAP default: what a single established mailbox was
# effectively limited to before per-mailbox caps existed.
_LEGACY_ORG_CAP = 200


def upgrade() -> None:
    # Only touch rows that still carry the stamped default, so a cap somebody
    # has already tuned is never overwritten.
    op.execute(
        sa.text(
            "UPDATE mailbox_connections SET daily_send_cap = :legacy "
            "WHERE daily_send_cap = :stamped"
        ).bindparams(legacy=_LEGACY_ORG_CAP, stamped=_NEW_MAILBOX_CAP)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE mailbox_connections SET daily_send_cap = :stamped "
            "WHERE daily_send_cap = :legacy"
        ).bindparams(stamped=_NEW_MAILBOX_CAP, legacy=_LEGACY_ORG_CAP)
    )
