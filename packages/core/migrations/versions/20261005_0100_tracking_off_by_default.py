"""tracking off by default

Switches Track opens / Track clicks off on every existing sequence and makes
off the default for new ones. The hidden pixel and the /t/c link rewrite are
bulk-mail signals, and these emails go out from a person's own mailbox to read
as personal mail. The switches stay, so one sequence can still opt back in.

The /t routes are untouched: emails already in recipients' inboxes still
carry the old pixel and links, and those links must keep resolving.

Revision ID: d1a4f7b2e9c3
Revises: c9e6a3b4d7f8
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1a4f7b2e9c3"
down_revision = "c9e6a3b4d7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE sequences SET track_opens = false, track_clicks = false"))
    with op.batch_alter_table("sequences") as batch:
        batch.alter_column("track_opens", server_default=sa.false())
        batch.alter_column("track_clicks", server_default=sa.false())


def downgrade() -> None:
    # Restores the old default only. Which sequences had tracking on before
    # the upgrade is not recorded, so no row is switched back on.
    with op.batch_alter_table("sequences") as batch:
        batch.alter_column("track_opens", server_default=sa.true())
        batch.alter_column("track_clicks", server_default=sa.true())
