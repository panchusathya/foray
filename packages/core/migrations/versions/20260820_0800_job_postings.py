"""Jobs database: postings, matches, watermarks, scrape queries, project.company.

Revision ID: b8c2e91f04d7
Revises: e2b6d4c17f90
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c2e91f04d7"
down_revision: str | None = "e2b6d4c17f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _embedding_type():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from pgvector.sqlalchemy import Vector

        return Vector()
    return sa.JSON()


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("company", sa.String(length=240), server_default="", nullable=False),
    )
    # Match the model (no server_default) after backfilling existing rows.
    with op.batch_alter_table("projects") as batch:
        batch.alter_column("company", server_default=None)

    embedding = _embedding_type()
    op.create_table(
        "job_postings",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("company", sa.String(length=240), nullable=False),
        sa.Column("company_normalized", sa.String(length=240), nullable=False),
        sa.Column("company_domain", sa.String(length=240), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("title_normalized", sa.String(length=240), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=False),
        sa.Column("open_to_remote", sa.Boolean(), nullable=False),
        sa.Column("seniority", sa.String(length=40), nullable=False),
        sa.Column("jd_text", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("parsed_spec", sa.JSON(), nullable=False),
        sa.Column("spec_version", sa.String(length=64), nullable=False),
        sa.Column("parser", sa.String(length=32), nullable=False),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding", embedding, nullable=True),
        sa.Column("embedding_model", sa.String(length=80), nullable=False),
        sa.Column("fee_bearing", sa.Boolean(), nullable=False),
        sa.Column("fee_terms", sa.String(length=240), nullable=False),
        sa.Column("fee_evidence", sa.Text(), nullable=False),
        sa.Column("referral_paths", sa.JSON(), nullable=False),
        sa.Column("has_warm_path", sa.Boolean(), nullable=False),
        sa.Column("match_count", sa.Integer(), nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matched_spec_version", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "source_id", name="uq_job_posting_source_id"),
    )
    op.create_index(op.f("ix_job_postings_org_id"), "job_postings", ["org_id"], unique=False)
    op.create_index(op.f("ix_job_postings_project_id"), "job_postings", ["project_id"], unique=False)
    op.create_index("ix_job_posting_org_status", "job_postings", ["org_id", "status"], unique=False)

    op.create_table(
        "job_matches",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("job_posting_id", sa.String(length=40), nullable=False),
        sa.Column("candidate_id", sa.String(length=40), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("heuristic_score", sa.Float(), nullable=False),
        sa.Column("vector_score", sa.Float(), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("dismissed_reason", sa.String(length=120), nullable=False),
        sa.Column("spec_version", sa.String(length=64), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_posting_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_posting_id", "candidate_id", name="uq_job_match"),
    )
    op.create_index(op.f("ix_job_matches_org_id"), "job_matches", ["org_id"], unique=False)
    op.create_index(op.f("ix_job_matches_job_posting_id"), "job_matches", ["job_posting_id"], unique=False)
    op.create_index(op.f("ix_job_matches_candidate_id"), "job_matches", ["candidate_id"], unique=False)
    op.create_index(
        "ix_job_match_candidate_score",
        "job_matches",
        ["org_id", "candidate_id", "score"],
        unique=False,
    )
    op.create_index(
        "ix_job_match_posting_score",
        "job_matches",
        ["job_posting_id", "score"],
        unique=False,
    )

    op.create_table(
        "job_match_watermarks",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("candidate_id", sa.String(length=40), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "candidate_id", name="uq_job_match_watermark"),
    )
    op.create_index(
        op.f("ix_job_match_watermarks_org_id"), "job_match_watermarks", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_job_match_watermarks_candidate_id"),
        "job_match_watermarks",
        ["candidate_id"],
        unique=False,
    )

    op.create_table(
        "job_scrape_queries",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("cadence_minutes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_found", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_job_scrape_queries_org_id"), "job_scrape_queries", ["org_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_job_scrape_queries_org_id"), table_name="job_scrape_queries")
    op.drop_table("job_scrape_queries")
    op.drop_index(op.f("ix_job_match_watermarks_candidate_id"), table_name="job_match_watermarks")
    op.drop_index(op.f("ix_job_match_watermarks_org_id"), table_name="job_match_watermarks")
    op.drop_table("job_match_watermarks")
    op.drop_index("ix_job_match_posting_score", table_name="job_matches")
    op.drop_index("ix_job_match_candidate_score", table_name="job_matches")
    op.drop_index(op.f("ix_job_matches_candidate_id"), table_name="job_matches")
    op.drop_index(op.f("ix_job_matches_job_posting_id"), table_name="job_matches")
    op.drop_index(op.f("ix_job_matches_org_id"), table_name="job_matches")
    op.drop_table("job_matches")
    op.drop_index("ix_job_posting_org_status", table_name="job_postings")
    op.drop_index(op.f("ix_job_postings_project_id"), table_name="job_postings")
    op.drop_index(op.f("ix_job_postings_org_id"), table_name="job_postings")
    op.drop_table("job_postings")
    op.drop_column("projects", "company")
