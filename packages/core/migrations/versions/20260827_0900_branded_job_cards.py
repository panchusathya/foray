"""Company brand assets, and the rendered job card that uses them.

``company_brands`` caches one company's logo and quantised palette by domain.
It is the only table in this schema with **no org_id**, and that is deliberate:
it holds publicly-fetched marketing assets keyed by a public domain, nothing in
it belongs to a tenant, and an org column would mean fetching the same logo
once per tenant. A row exists for a miss as well as a hit -- ``error`` set with
``fetched_at`` stamped -- so a company with no discoverable logo is not
refetched on every card.

``job_card_assets`` caches the rendered PNG and Linq's attachment id, so a role
proposed to thirty candidates renders and uploads once. ``card_version`` is
what lets a renderer change reach a posting somebody has already been shown.

Revision ID: c7f31e0b6a94
Revises: b6e2d41a90f7
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7f31e0b6a94"
down_revision: str | None = "b6e2d41a90f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_brands",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("domain", sa.String(length=240), nullable=False),
        sa.Column("logo_path", sa.String(length=400), server_default="", nullable=False),
        sa.Column("palette", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=24), server_default="", nullable=False),
        sa.Column("error", sa.String(length=400), server_default="", nullable=False),
        sa.Column("brand_version", sa.String(length=24), server_default="", nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("domain", name="uq_company_brand_domain"),
    )
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


def downgrade() -> None:
    op.drop_index("ix_job_card_assets_job_posting_id", table_name="job_card_assets")
    op.drop_index("ix_job_card_assets_org_id", table_name="job_card_assets")
    op.drop_table("job_card_assets")
    op.drop_table("company_brands")
