"""A derived FormSpec cached on the posting, for boards nobody mapped.

An unknown board used to be a dead end: no handwritten map, no adapter, never
carded. ``submit/browser/derive.py`` now reads a spec off the posting's own
page, and it lives here -- on the posting, not the application -- because the
carding gate consults it and one page view per posting amortises across every
candidate who matches it.

Two definitive shapes: a usable spec (has "fields") or ``{"unusable": reason}``.
NULL means never checked. A transient failure writes nothing at all, and
``form_spec_checked_at`` is stamped only with a definitive answer -- the
``resolve_target`` stamp_on_miss lesson, so an aggregator 403ing a datacentre
IP today does not cache "no form here" forever.

Revision ID: a7d3f92c48e1
Revises: f3c9a2e64d17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3f92c48e1"
down_revision: str | None = "f3c9a2e64d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_postings", sa.Column("form_spec", sa.JSON(), nullable=True))
    op.add_column(
        "job_postings",
        sa.Column("form_spec_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_postings", "form_spec_checked_at")
    op.drop_column("job_postings", "form_spec")
