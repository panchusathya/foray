"""Staging for contacts discovered by sweeping Sent Items.

The live thread sync baselines at connect time and reads no history, and the
existing importer is per-candidate, so nobody new can ever be found. This adds
the table a sweep stages people into, plus somewhere to keep its progress.

**No subject column, no body column.** These rows describe people who have not
been reviewed, let alone opted into; the tray only needs "5 messages, last one
14 months ago" to triage on. A subject line would be genuinely useful and is
exactly the thing that breaks the promise, so the absence is asserted by a test
rather than left to discipline.

``discovery_state`` on the connection holds the sweep's counters *and* its page
cursor. The cursor cannot live in the job payload -- the job re-enqueues itself
per page -- and must not go anywhere near ``inbox_delta_link`` or
``sent_delta_link``: overwriting a live delta link makes the ongoing sync
permanently re-read or permanently skip mail.

Inert on deploy. Nothing writes these until a sweep is started by hand.

Revision ID: c7f2a05e83b1
Revises: b8c3e91d40a7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c7f2a05e83b1"
down_revision: str | None = "b8c3e91d40a7"
branch_labels = None
depends_on = None


def _recreate() -> str:
    # SQLite cannot ALTER COLUMN; Postgres must not recreate mailbox_connections
    # because enrollments FKs into it with ON DELETE RESTRICT.
    return "always" if op.get_bind().dialect.name == "sqlite" else "never"


def upgrade() -> None:
    op.create_table(
        "discovered_contacts",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("domain", sa.String(255), nullable=False, server_default=""),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outbound_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("to_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cc_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("thread_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mailbox_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("suggested_status", sa.String(24), nullable=False, server_default=""),
        sa.Column(
            "candidate_id",
            sa.String(40),
            sa.ForeignKey("candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_reason", sa.String(60), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "email", name="uq_discovered_org_email"),
    )
    op.create_index("ix_discovered_contacts_org_id", "discovered_contacts", ["org_id"])
    op.create_index(
        "ix_discovered_org_status_seen",
        "discovered_contacts",
        ["org_id", "status", "last_seen_at"],
    )
    # Clear the defaults so the migrated schema matches the models and
    # test_migrated_schema_matches_the_models stays green.
    with op.batch_alter_table("discovered_contacts") as batch:
        for column in (
            "display_name",
            "domain",
            "outbound_count",
            "to_count",
            "cc_count",
            "thread_count",
            "mailbox_ids",
            "status",
            "suggested_status",
            "dismissed_reason",
        ):
            batch.alter_column(column, server_default=None)

    with op.batch_alter_table("mailbox_connections", recreate=_recreate()) as batch:
        batch.add_column(
            sa.Column("discovery_state", sa.JSON(), nullable=False, server_default="{}")
        )
    with op.batch_alter_table("mailbox_connections", recreate=_recreate()) as batch:
        batch.alter_column("discovery_state", server_default=None)

    # Extra corporate domains that count as "us". Derived ones come from the
    # connected mailboxes; this is for a firm with several domains, and for the
    # tray's one-click "ignore this vendor".
    with op.batch_alter_table("orgs", recreate=_recreate()) as batch:
        batch.add_column(
            sa.Column("crm_internal_domains", sa.JSON(), nullable=False, server_default="[]")
        )
    with op.batch_alter_table("orgs", recreate=_recreate()) as batch:
        batch.alter_column("crm_internal_domains", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("orgs", recreate=_recreate()) as batch:
        batch.drop_column("crm_internal_domains")

    with op.batch_alter_table("mailbox_connections", recreate=_recreate()) as batch:
        batch.drop_column("discovery_state")

    op.drop_index("ix_discovered_org_status_seen", table_name="discovered_contacts")
    op.drop_index("ix_discovered_contacts_org_id", table_name="discovered_contacts")
    op.drop_table("discovered_contacts")
