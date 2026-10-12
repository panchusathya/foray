"""deliverability seeds

Seed inboxes we own, the seed tests sent to them from each sending mailbox,
the latest DNS / blocklist read per sending domain, and the org's seed-test
cadence. Measurement only: nothing here changes how outreach is sent, except
that a seed send counts against its mailbox's daily cap.

Revision ID: e3b7c1d9a4f2
Revises: d1a4f7b2e9c3
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e3b7c1d9a4f2"
down_revision = "d1a4f7b2e9c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orgs") as batch:
        batch.add_column(
            sa.Column(
                "seed_test_interval_hours", sa.Integer(), nullable=False, server_default="24"
            )
        )

    op.create_table(
        "seed_inboxes",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "org_id", sa.String(40), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("address", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="imap"),
        sa.Column("provider_label", sa.String(20), nullable=False, server_default="other"),
        sa.Column("imap_host", sa.String(255), nullable=False, server_default=""),
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("username", sa.String(255), nullable=False, server_default=""),
        sa.Column("secret_blob", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("spam_visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "address", name="uq_seed_inbox_org_address"),
    )
    op.create_index("ix_seed_inboxes_org_id", "seed_inboxes", ["org_id"])

    op.create_table(
        "seed_tests",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "org_id", sa.String(40), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "mailbox_id",
            sa.String(40),
            sa.ForeignKey("mailbox_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "seed_id",
            sa.String(40),
            sa.ForeignKey("seed_inboxes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_key", sa.String(200), nullable=False),
        sa.Column(
            "source_message_id",
            sa.String(40),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("subject", sa.String(500), nullable=False, server_default=""),
        sa.Column("from_address", sa.String(255), nullable=False, server_default=""),
        sa.Column("internet_message_id", sa.String(300), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("folder", sa.String(120), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "job_key", name="uq_seed_test_job_key"),
    )
    op.create_index("ix_seed_tests_org_id", "seed_tests", ["org_id"])
    op.create_index("ix_seed_tests_mailbox_sent", "seed_tests", ["mailbox_id", "sent_at"])
    op.create_index("ix_seed_tests_seed_status", "seed_tests", ["seed_id", "status"])

    op.create_table(
        "domain_checks",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "org_id", sa.String(40), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "domain", name="uq_domain_check_org_domain"),
    )
    op.create_index("ix_domain_checks_org_id", "domain_checks", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_domain_checks_org_id", table_name="domain_checks")
    op.drop_table("domain_checks")
    op.drop_index("ix_seed_tests_seed_status", table_name="seed_tests")
    op.drop_index("ix_seed_tests_mailbox_sent", table_name="seed_tests")
    op.drop_index("ix_seed_tests_org_id", table_name="seed_tests")
    op.drop_table("seed_tests")
    op.drop_index("ix_seed_inboxes_org_id", table_name="seed_inboxes")
    op.drop_table("seed_inboxes")
    with op.batch_alter_table("orgs") as batch:
        batch.drop_column("seed_test_interval_hours")
