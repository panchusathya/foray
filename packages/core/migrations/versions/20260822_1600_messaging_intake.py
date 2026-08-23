"""Messaging intake: threads, messages, and a second provenance link.

The sibling of ``20260822_1200_voice_intake``, and deliberately not an
extension of it. ``intake_calls`` models a bounded session -- it has a
duration, a recording URL and one transcript that lands at the end.
``intake_threads`` models a conversation that may take a week, has no
duration, and is re-read before every outbound message. Adding a ``channel``
column to ``intake_calls`` instead would leave half the columns null on
whichever side you are looking at, and renaming that table while the voice
branch is still writing to it is a guaranteed conflict. Folding both into an
``intake_sessions`` view is a fair refactor once both branches have landed.

``intake_threads.record`` is the column with no counterpart on the call side.
A call extracts once from a finished transcript; a thread has to answer "what
do we still owe this person?" before every message it sends, so the merged
record is stored and rewritten each turn rather than recomputed from scratch
at the end.

Two unique keys, both partial, both for the same reason as
``messages.provider_native_id``: inbound webhooks are retried as a matter of
course, so the sender's own id is the idempotency key -- but our own outbound
rows have no provider id, and blank must not collide with blank. The message
key is scoped to the *thread* rather than the org, unlike its precedent: a
mail provider's id is unique across a tenant, but a web chat's comes from the
browser, and two strangers both sending "1" must not silently swallow each
other's messages.

``candidate_preferences.source_thread_id`` is added alongside
``source_call_id`` rather than replacing it with a polymorphic
``source_type``/``source_id`` pair. A real foreign key per channel keeps the
cascade honest and the join readable; exactly one of the two is ever set. Both
are ``SET NULL``, so deleting a conversation drops the provenance and keeps
the preferences -- the preferences are what matching reads, and losing them
because a transcript was purged would be the wrong trade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2c94af013"
down_revision: str | None = "c1a7f4e05b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # intake_threads first: intake_messages and candidate_preferences both
    # point at it.
    op.create_table(
        "intake_threads",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel", sa.String(length=16), server_default="web", nullable=False),
        sa.Column("provider", sa.String(length=32), server_default="native", nullable=False),
        sa.Column(
            "provider_thread_id", sa.String(length=200), server_default="", nullable=False
        ),
        sa.Column(
            "entry_point", sa.String(length=24), server_default="web_widget", nullable=False
        ),
        sa.Column("from_address", sa.String(length=64), server_default="", nullable=False),
        sa.Column("to_address", sa.String(length=64), server_default="", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column(
            "consent_messaging", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("contact_name", sa.String(length=240), server_default="", nullable=False),
        sa.Column("contact_email", sa.String(length=255), server_default="", nullable=False),
        sa.Column("record", sa.JSON(), nullable=False),
        sa.Column("extractor", sa.String(length=32), server_default="", nullable=False),
        sa.Column("schema_version", sa.String(length=32), server_default="", nullable=False),
        sa.Column(
            "required_complete", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("fields_missing", sa.JSON(), nullable=False),
        sa.Column("low_confidence", sa.JSON(), nullable=False),
        sa.Column("review_needed", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("review_reason", sa.String(length=400), server_default="", nullable=False),
        sa.Column("last_field_asked", sa.String(length=64), server_default="", nullable=False),
        sa.Column("asked_counts", sa.JSON(), nullable=False),
        sa.Column("inbound_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("outbound_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=400), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_intake_threads_org_id"), "intake_threads", ["org_id"])
    op.create_index(op.f("ix_intake_threads_candidate_id"), "intake_threads", ["candidate_id"])
    op.create_index("ix_intake_threads_org_status", "intake_threads", ["org_id", "status"])
    op.create_index("ix_intake_threads_org_created", "intake_threads", ["org_id", "created_at"])
    op.create_index(
        "ix_intake_threads_channel_from", "intake_threads", ["channel", "from_address"]
    )
    # Partial: the web channel mints no external conversation id, so blank is
    # the common case and must not be unique.
    op.create_index(
        "uq_intake_threads_provider_id",
        "intake_threads",
        ["provider", "provider_thread_id"],
        unique=True,
        sqlite_where=sa.text("provider_thread_id != ''"),
        postgresql_where=sa.text("provider_thread_id != ''"),
    )

    op.create_table(
        "intake_messages",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            sa.String(length=40),
            sa.ForeignKey("intake_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), server_default="0", nullable=False),
        sa.Column("direction", sa.String(length=16), server_default="inbound", nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "provider_native_id", sa.String(length=300), server_default="", nullable=False
        ),
        sa.Column("asked_field", sa.String(length=64), server_default="", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="received", nullable=False),
        sa.Column("error", sa.String(length=400), server_default="", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("thread_id", "seq", name="uq_intake_message_seq"),
    )
    op.create_index(op.f("ix_intake_messages_org_id"), "intake_messages", ["org_id"])
    op.create_index("ix_intake_messages_thread", "intake_messages", ["thread_id", "seq"])
    op.create_index(
        "uq_intake_messages_thread_native_id",
        "intake_messages",
        ["thread_id", "provider_native_id"],
        unique=True,
        sqlite_where=sa.text("provider_native_id != ''"),
        postgresql_where=sa.text("provider_native_id != ''"),
    )

    # Batch mode: SQLite rebuilds the table to gain the FK; Postgres ALTERs.
    with op.batch_alter_table("candidate_preferences") as batch:
        batch.add_column(
            sa.Column(
                "source_thread_id",
                sa.String(length=40),
                sa.ForeignKey(
                    "intake_threads.id",
                    ondelete="SET NULL",
                    name="fk_candidate_preferences_source_thread_id",
                ),
                nullable=True,
            )
        )
    op.create_index(
        op.f("ix_candidate_preferences_source_thread_id"),
        "candidate_preferences",
        ["source_thread_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_candidate_preferences_source_thread_id"), table_name="candidate_preferences"
    )
    with op.batch_alter_table("candidate_preferences") as batch:
        batch.drop_column("source_thread_id")

    op.drop_index("uq_intake_messages_thread_native_id", table_name="intake_messages")
    op.drop_index("ix_intake_messages_thread", table_name="intake_messages")
    op.drop_index(op.f("ix_intake_messages_org_id"), table_name="intake_messages")
    op.drop_table("intake_messages")

    op.drop_index("uq_intake_threads_provider_id", table_name="intake_threads")
    op.drop_index("ix_intake_threads_channel_from", table_name="intake_threads")
    op.drop_index("ix_intake_threads_org_created", table_name="intake_threads")
    op.drop_index("ix_intake_threads_org_status", table_name="intake_threads")
    op.drop_index(op.f("ix_intake_threads_candidate_id"), table_name="intake_threads")
    op.drop_index(op.f("ix_intake_threads_org_id"), table_name="intake_threads")
    op.drop_table("intake_threads")
