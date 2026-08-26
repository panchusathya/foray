"""Short portal links: a code on the grant row instead of a JWT in the URL.

A texted magic link was ``/m/<jwt>`` -- several hundred characters of bearer
credential at the end of every message. The grant row was already what made the
link single-use (``consumed_at``), so the signature was doing no work the row
was not; ``link_code`` is 80 bits of ``secrets`` and the row does the rest.

``next_path`` was a claim inside that token. With the token gone there is
nowhere else to put it, and it does not belong in a query string: a URL that
gets forwarded should not carry where somebody was going.

The unique index on ``link_code`` **must be partial**. OTP and recruiter grants
never appear in a URL and carry '', SQL compares '' to '' as equal, and a plain
unique index would therefore let the second such grant ever issued collide with
the first. Same trap as the tracker's ``provider_native_id`` index.

Revision ID: e2f8b4a17c93
Revises: c4a91e7d3b62
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f8b4a17c93"
down_revision: str | None = "c4a91e7d3b62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table because SQLite cannot ALTER TABLE ... ADD CONSTRAINT,
    # and test_migrations runs this against SQLite.
    with op.batch_alter_table("candidate_auth_grants") as batch:
        batch.add_column(
            sa.Column("link_code", sa.String(length=64), server_default="", nullable=False)
        )
        batch.add_column(
            sa.Column("next_path", sa.String(length=400), server_default="/", nullable=False)
        )
    op.create_index(
        "uq_candidate_auth_grant_code",
        "candidate_auth_grants",
        ["link_code"],
        unique=True,
        sqlite_where=sa.text("link_code != ''"),
        postgresql_where=sa.text("link_code != ''"),
    )


def downgrade() -> None:
    op.drop_index("uq_candidate_auth_grant_code", table_name="candidate_auth_grants")
    with op.batch_alter_table("candidate_auth_grants") as batch:
        batch.drop_column("next_path")
        batch.drop_column("link_code")
