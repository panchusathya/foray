"""Project candidates.emails into a table so an address can be looked up.

``candidates.emails`` is a JSON list, so "who owns this address?" cannot be a
join. ``build_address_index`` works around that by loading every in-scope
candidate into a Python dict, which is fine once per sync job and not fine
inside a single HTTP request -- which is exactly what the manual-add duplicate
check needs, and what the discovery sweep will need to tell a stranger from
somebody already in Contacts.

The unique constraint states a rule the codebase already relies on but has
never enforced: one address belongs to one person. ``build_address_index``
resolves collisions by iteration order and calls it "first writer wins", which
is a comment, not a guarantee.

**The backfill can therefore not be a straight copy.** Nothing has ever stopped
the same address appearing in two candidates' JSON lists, and on real data some
almost certainly do. Rather than fail the migration or pick a winner
arbitrarily at query time, the backfill inserts the first candidate to claim an
address (ordered by id, so it is deterministic and re-runnable) and skips the
rest; the losers keep their JSON copy untouched. Nothing is deleted and no
existing behaviour changes -- ``build_address_index`` still reads the JSON.

The consequence worth writing down: **the projection is a subset of the JSON,
not a mirror of it.** A test may assert that every projection row matches some
candidate's ``emails``; asserting the converse would fail on the first shared
address in production.

Revision ID: b8c3e91d40a7
Revises: a4f10c7b62d8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b8c3e91d40a7"
down_revision: str | None = "a4f10c7b62d8"
branch_labels = None
depends_on = None

# Matches enrich.normalize.clean_emails, which is what wrote the JSON.
_BATCH = 1000


def upgrade() -> None:
    op.create_table(
        "candidate_emails",
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
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("org_id", "email", name="uq_candidate_email_org"),
    )
    op.create_index("ix_candidate_emails_org_id", "candidate_emails", ["org_id"])
    op.create_index("ix_candidate_emails_candidate_id", "candidate_emails", ["candidate_id"])
    op.create_index("ix_candidate_emails_candidate", "candidate_emails", ["candidate_id"])
    with op.batch_alter_table("candidate_emails") as batch:
        batch.alter_column("position", server_default=None)

    _backfill()


def _backfill() -> None:
    """Expand the JSON lists into rows, in id order, first claim wins.

    Done in Python because there is no portable SQL for "expand a JSON array
    into rows" across SQLite and Postgres, and chunked because a 200k-row pool
    should not be held in one statement.
    """
    bind = op.get_bind()
    candidates = sa.table(
        "candidates",
        sa.column("id", sa.String),
        sa.column("org_id", sa.String),
        sa.column("emails", sa.JSON),
    )
    rows = bind.execute(
        sa.select(candidates.c.id, candidates.c.org_id, candidates.c.emails).order_by(
            candidates.c.id
        )
    ).fetchall()

    # (org_id, email) already claimed in this run.
    claimed: set[tuple[str, str]] = set()
    pending: list[dict] = []
    counter = 0

    for candidate_id, org_id, emails in rows:
        if not emails:
            continue
        for position, raw in enumerate(emails):
            email = (raw or "").strip().lower()
            if not email:
                continue
            key = (org_id, email)
            if key in claimed:
                # Someone earlier already owns it. They keep it; this
                # candidate keeps their JSON copy and gets no row.
                continue
            claimed.add(key)
            counter += 1
            pending.append(
                {
                    "id": f"cem_{counter:024x}",
                    "org_id": org_id,
                    "candidate_id": candidate_id,
                    "email": email,
                    "position": position,
                }
            )
        if len(pending) >= _BATCH:
            _flush(bind, pending)
            pending = []
    _flush(bind, pending)


def _flush(bind, pending: list[dict]) -> None:
    if not pending:
        return
    bind.execute(
        sa.text(
            "INSERT INTO candidate_emails (id, org_id, candidate_id, email, position) "
            "VALUES (:id, :org_id, :candidate_id, :email, :position)"
        ),
        pending,
    )


def downgrade() -> None:
    op.drop_index("ix_candidate_emails_candidate", table_name="candidate_emails")
    op.drop_index("ix_candidate_emails_candidate_id", table_name="candidate_emails")
    op.drop_index("ix_candidate_emails_org_id", table_name="candidate_emails")
    op.drop_table("candidate_emails")
