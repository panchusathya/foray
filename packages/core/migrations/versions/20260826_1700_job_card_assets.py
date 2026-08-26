"""Rendered job-card images, and the protocol a thread last delivered over.

Two things, both in service of putting a real picture of the role in the
message rather than a contact card the candidate has to tap.

``job_card_assets`` is a cache with a version on it. A role proposed to thirty
candidates is one card, so the bytes are rendered and uploaded to Linq once and
the durable ``attachment_id`` is reused. ``card_version`` is what makes the
cache safe to keep: without it a renderer or palette change would never reach a
posting somebody had already been shown, and the cache would outlive the code
that filled it.

``intake_threads.last_service`` is how we know an image will render inline
instead of becoming an MMS. Empty is a real state and not a synonym for SMS --
we have simply never been told -- so presentation falls back to text for both,
because guessing either way is wrong about half the time.

Revision ID: c4a91e7d3b62
Revises: a9d47c31e8b5
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a91e7d3b62"
down_revision: str | None = "a9d47c31e8b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_card_assets",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_posting_id",
            sa.String(length=40),
            sa.ForeignKey("job_postings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("variant", sa.String(length=24), server_default="card", nullable=False),
        sa.Column("storage_path", sa.String(length=400), server_default="", nullable=False),
        sa.Column("attachment_id", sa.String(length=120), server_default="", nullable=False),
        sa.Column("card_version", sa.String(length=40), server_default="", nullable=False),
        sa.Column("rendered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "job_posting_id", "variant", name="uq_job_card_asset"),
    )
    op.create_index("ix_job_card_assets_org_id", "job_card_assets", ["org_id"])
    op.create_index(
        "ix_job_card_assets_job_posting_id", "job_card_assets", ["job_posting_id"]
    )

    # batch_alter_table, not a bare add_column: SQLite cannot ALTER TABLE ...
    # ADD CONSTRAINT, and test_migrations runs this against SQLite. The
    # convention is already set by 20260818_0930_mailbox_connections.py.
    with op.batch_alter_table("intake_threads") as batch:
        batch.add_column(
            sa.Column(
                "last_service",
                sa.String(length=16),
                server_default="",
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("intake_threads") as batch:
        batch.drop_column("last_service")
    op.drop_index("ix_job_card_assets_job_posting_id", table_name="job_card_assets")
    op.drop_index("ix_job_card_assets_org_id", table_name="job_card_assets")
    op.drop_table("job_card_assets")
