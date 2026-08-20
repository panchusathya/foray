"""The CRM record: a per-person status, hand-edit protection, notes and tags.

Three additions to ``candidates`` and two new tables. No data moves, and
nothing existing changes shape, so this is safe to deploy ahead of the code
that reads it.

``crm_status`` is a per-person fact, deliberately not the same axis as
``project_candidates.stage``: stage is per-role and partly outreach-driven
(see OUTREACH_DRIVEN_STAGES), while this is one hand-set statement about the
relationship. It is a plain string validated in the router rather than a
Postgres enum, because adding a value to an enum needs its own migration.

``crm_tracked`` says the person is in the CRM on purpose rather than merely
sourced. It exists because a promoted or hand-added contact satisfies none of
the three conditions in integrations/matching.py::build_address_index -- no
project, no sequence, no prior message -- so their mail would keep being
discarded after the user explicitly asked to track them. Defaulting it to
false is what keeps this migration inert: the privacy guard does not widen for
a single existing row until something sets the flag.

``manual_fields`` records which fields a human edited, so repository._apply can
re-apply them over imported data. Without it the nightly Foray refresh silently
reverts every correction, which is the kind of bug nobody reports because it
looks like the edit never saved.

``candidate_notes`` is a person-scoped entity and does not replace
``project_candidates.notes``, which is a per-role scratch field on a pipeline
card. Two different scopes; merging them would mean dual writes.

``candidate_tags`` is a join table rather than a JSON list because tags are a
grid filter. ``candidates.skills`` already shows the alternative: a JSON
multi-value column cannot be indexed and ends up filtered in Python after
LIMIT/OFFSET, which silently breaks paging.

Revision ID: a4f10c7b62d8
Revises: d1e4f70b93ac
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a4f10c7b62d8"
down_revision: str | None = "d1e4f70b93ac"
branch_labels = None
depends_on = None


def _recreate() -> str:
    # SQLite cannot ALTER COLUMN, so it needs a full batch-recreate. Postgres
    # must NOT recreate: messages, enrollments, project_candidates,
    # search_results and now these two tables all FK into candidates, and
    # dropping its pkey mid-recreate fails with DependentObjectsStillExist.
    return "always" if op.get_bind().dialect.name == "sqlite" else "never"


def upgrade() -> None:
    recreate = _recreate()

    # ---- candidates: the CRM columns ------------------------------------
    # server_default is required on add (SQLite cannot add a NOT NULL column
    # without one) and then cleared, so the migrated schema matches the models
    # and test_migrated_schema_matches_the_models stays green.
    with op.batch_alter_table("candidates", recreate=recreate) as batch:
        batch.add_column(
            sa.Column("crm_status", sa.String(32), nullable=False, server_default="none")
        )
        batch.add_column(
            sa.Column("crm_tracked", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("manual_fields", sa.JSON(), nullable=False, server_default="[]")
        )
    with op.batch_alter_table("candidates", recreate=recreate) as batch:
        batch.alter_column("crm_status", server_default=None)
        batch.alter_column("crm_tracked", server_default=None)
        batch.alter_column("manual_fields", server_default=None)

    op.create_index("ix_candidate_org_status", "candidates", ["org_id", "crm_status"])
    op.create_index("ix_candidate_org_tracked", "candidates", ["org_id", "crm_tracked"])

    # ---- notes ----------------------------------------------------------
    op.create_table(
        "candidate_notes",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Unconstrained on purpose, like projects.created_by: an API-key
        # principal has no user row, and deleting a user must not delete the
        # note or blank its byline -- hence the author_name snapshot.
        sa.Column("author_id", sa.String(40), nullable=True),
        sa.Column("author_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_candidate_notes_org_id", "candidate_notes", ["org_id"])
    op.create_index("ix_candidate_notes_candidate_id", "candidate_notes", ["candidate_id"])
    op.create_index(
        "ix_candidate_notes_candidate", "candidate_notes", ["candidate_id", "created_at"]
    )
    with op.batch_alter_table("candidate_notes") as batch:
        batch.alter_column("author_name", server_default=None)

    # ---- tags -----------------------------------------------------------
    op.create_table(
        "candidate_tags",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tag", sa.String(60), nullable=False),
        sa.Column("created_by", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_id", "tag", name="uq_candidate_tag"),
    )
    op.create_index("ix_candidate_tags_org_id", "candidate_tags", ["org_id"])
    op.create_index("ix_candidate_tags_candidate_id", "candidate_tags", ["candidate_id"])
    op.create_index("ix_candidate_tags_org_tag", "candidate_tags", ["org_id", "tag"])


def downgrade() -> None:
    recreate = _recreate()

    op.drop_index("ix_candidate_tags_org_tag", table_name="candidate_tags")
    op.drop_index("ix_candidate_tags_candidate_id", table_name="candidate_tags")
    op.drop_index("ix_candidate_tags_org_id", table_name="candidate_tags")
    op.drop_table("candidate_tags")

    op.drop_index("ix_candidate_notes_candidate", table_name="candidate_notes")
    op.drop_index("ix_candidate_notes_candidate_id", table_name="candidate_notes")
    op.drop_index("ix_candidate_notes_org_id", table_name="candidate_notes")
    op.drop_table("candidate_notes")

    op.drop_index("ix_candidate_org_tracked", table_name="candidates")
    op.drop_index("ix_candidate_org_status", table_name="candidates")
    with op.batch_alter_table("candidates", recreate=recreate) as batch:
        batch.drop_column("manual_fields")
        batch.drop_column("crm_tracked")
        batch.drop_column("crm_status")
