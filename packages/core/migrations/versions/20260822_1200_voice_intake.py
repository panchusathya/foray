"""Voice intake: calls, candidate preferences, and a phone projection.

Three tables, one backfill.

``intake_calls`` records a call whether or not we can attribute it to anybody --
``candidate_id`` is nullable because a stranger on the public dial-in number has
no candidate row yet, and an unattributable call is precisely the one worth
looking at later. The unique key is ``(provider, provider_call_id)``: voice
webhooks are retried as a matter of course, and without it a retry extracts the
same conversation twice.

``candidate_preferences`` is the mirror of ``job_postings.parsed_spec`` -- the
same ``ParsedQuery`` shape through the same taxonomy, so ``jobmatch`` can score
what a company asked for against what a person said they want. It is a separate
table rather than columns on ``candidates`` because ``repository._apply``
re-applies ``manual_fields`` over imported data to stop a nightly Foray refresh
overwriting human corrections, and preferences need that immunity without
widening that mechanism over twenty more columns. Its booleans are nullable on
purpose: ``candidates.open_to_remote`` cannot distinguish "wants an office" from
"never asked", and that is the reason filtering on it is untrustworthy.

``candidate_phones`` projects ``candidates.phones`` the way ``candidate_emails``
projects ``candidates.emails``, and for the same reason -- a JSON list cannot
answer "who owns this?" with a join. The failure mode is sharper here, which is
why the key is stricter. A wrong address lookup finds nobody; a wrong *phone*
lookup finds the wrong person, and the dial-in agent then reads their salary
expectations to whoever dialled. So a row is created only for a number whose
country is actually knowable: already E.164, or North-American-shaped. Anything
else gets no row and its owner stays unknown to caller ID, which costs one extra
question on a call and leaks nothing.

The key function is copied into this file rather than imported from
``intake.phones``. A migration has to keep meaning what it meant when it ran,
and importing application code lets a later refactor silently change what this
backfill did.

Two consequences worth writing down. **The projection is a strict subset of the
column** -- both because a number can be unnormalisable and because
``UniqueConstraint(org_id, phone)`` makes one number belong to one person, so
the backfill is first-writer-wins (ordered by id, deterministic, re-runnable)
and the loser keeps their JSON copy with no row. And **two stored numbers can
collapse into one row**: "(415) 555-0123" and "+1 415 555 0123" are the same
key, which is one row rather than an integrity error. A test asserting every
projection row matches some candidate's ``phones`` will hold; asserting the
converse will not.

Revision ID: c1a7f4e05b93
Revises: f5a83c0d21e7
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1a7f4e05b93"
down_revision: str | None = "f5a83c0d21e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 1000

# Frozen copy of intake.phones.lookup_key at this revision. See the module
# docstring for why this is not an import.
_KEEP = re.compile(r"[^\d+]")
_DEFAULT_REGION_CODE = "1"


def _lookup_key(value: str | None) -> str:
    if not value:
        return ""
    cleaned = _KEEP.sub("", str(value))
    if not cleaned:
        return ""
    explicit = cleaned.startswith("+")
    digits = cleaned.lstrip("+").replace("+", "")
    if not digits:
        return ""
    if explicit:
        return f"+{digits}" if 8 <= len(digits) <= 15 else ""
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+{_DEFAULT_REGION_CODE}{digits}"
    return ""


def upgrade() -> None:
    # intake_calls first: candidate_preferences.source_call_id points at it.
    op.create_table(
        "intake_calls",
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
        sa.Column("provider", sa.String(length=32), server_default="retell", nullable=False),
        sa.Column("provider_call_id", sa.String(length=200), nullable=False),
        sa.Column("entry_point", sa.String(length=16), server_default="web", nullable=False),
        sa.Column("from_number", sa.String(length=32), server_default="", nullable=False),
        sa.Column("to_number", sa.String(length=32), server_default="", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="in_progress", nullable=False),
        sa.Column(
            "consent_recording", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("transcript", sa.Text(), server_default="", nullable=False),
        sa.Column("recording_url", sa.Text(), server_default="", nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("extracted", sa.JSON(), nullable=False),
        sa.Column("extractor", sa.String(length=32), server_default="", nullable=False),
        sa.Column("schema_version", sa.String(length=32), server_default="", nullable=False),
        sa.Column(
            "required_complete", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("fields_missing", sa.JSON(), nullable=False),
        sa.Column("low_confidence", sa.JSON(), nullable=False),
        sa.Column("review_needed", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("review_reason", sa.String(length=400), server_default="", nullable=False),
        sa.Column("error", sa.String(length=400), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "provider", "provider_call_id", name="uq_intake_call_provider_id"
        ),
    )
    op.create_index("ix_intake_calls_org_id", "intake_calls", ["org_id"])
    op.create_index("ix_intake_calls_candidate_id", "intake_calls", ["candidate_id"])
    op.create_index("ix_intake_calls_org_created", "intake_calls", ["org_id", "created_at"])
    op.create_index("ix_intake_calls_org_review", "intake_calls", ["org_id", "review_needed"])

    op.create_table(
        "candidate_preferences",
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
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_call_id",
            sa.String(length=40),
            sa.ForeignKey("intake_calls.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), server_default="", nullable=False),
        sa.Column(
            "employment_status", sa.String(length=32), server_default="", nullable=False
        ),
        sa.Column("reason_for_looking", sa.Text(), server_default="", nullable=False),
        sa.Column("timeline", sa.String(length=32), server_default="", nullable=False),
        sa.Column("urgency_driver", sa.Text(), server_default="", nullable=False),
        sa.Column("target_base", sa.Float(), nullable=True),
        sa.Column("target_total", sa.Float(), nullable=True),
        sa.Column("comp_currency", sa.String(length=8), server_default="", nullable=False),
        sa.Column("comp_flexibility_notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "equity_importance", sa.String(length=16), server_default="", nullable=False
        ),
        sa.Column(
            "work_authorization", sa.String(length=120), server_default="", nullable=False
        ),
        sa.Column("willing_to_relocate", sa.Boolean(), nullable=True),
        sa.Column("active_processes", sa.JSON(), nullable=False),
        sa.Column("offers_in_hand", sa.JSON(), nullable=False),
        sa.Column("highlights", sa.JSON(), nullable=False),
        sa.Column("confidential_items", sa.JSON(), nullable=False),
        sa.Column("ok_to_share_with_clients", sa.Boolean(), nullable=True),
        sa.Column("declined", sa.JSON(), nullable=False),
        sa.Column("low_confidence", sa.JSON(), nullable=False),
        sa.Column("fields_missing", sa.JSON(), nullable=False),
        sa.Column("review_needed", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("review_reason", sa.String(length=400), server_default="", nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.String(length=32), server_default="", nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_id", name="uq_candidate_preference"),
    )
    op.create_index("ix_candidate_preferences_org_id", "candidate_preferences", ["org_id"])
    op.create_index(
        "ix_candidate_preferences_candidate_id", "candidate_preferences", ["candidate_id"]
    )
    op.create_index(
        "ix_candidate_preferences_source_call_id", "candidate_preferences", ["source_call_id"]
    )
    op.create_index("ix_candidate_preferences_org", "candidate_preferences", ["org_id"])
    op.create_index(
        "ix_candidate_preferences_org_review",
        "candidate_preferences",
        ["org_id", "review_needed"],
    )

    op.create_table(
        "candidate_phones",
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
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.UniqueConstraint("org_id", "phone", name="uq_candidate_phone_org"),
    )
    op.create_index("ix_candidate_phones_org_id", "candidate_phones", ["org_id"])
    op.create_index("ix_candidate_phones_candidate_id", "candidate_phones", ["candidate_id"])
    op.create_index("ix_candidate_phones_candidate", "candidate_phones", ["candidate_id"])
    with op.batch_alter_table("candidate_phones") as batch:
        batch.alter_column("position", server_default=None)

    _backfill_phones()


def _backfill_phones() -> None:
    """Expand candidates.phones into rows, in id order, first claim wins.

    Python rather than SQL because there is no portable way to expand a JSON
    array into rows across SQLite and Postgres, and chunked so a large pool is
    not held in one statement.
    """
    bind = op.get_bind()
    candidates = sa.table(
        "candidates",
        sa.column("id", sa.String),
        sa.column("org_id", sa.String),
        sa.column("phones", sa.JSON),
    )
    rows = bind.execute(
        sa.select(candidates.c.id, candidates.c.org_id, candidates.c.phones).order_by(
            candidates.c.id
        )
    ).fetchall()

    claimed: set[tuple[str, str]] = set()
    pending: list[dict] = []
    counter = 0

    for candidate_id, org_id, phones in rows:
        if not phones:
            continue
        seen_for_candidate: set[str] = set()
        position = 0
        for raw in phones:
            key = _lookup_key(raw)
            if not key:
                # Country not knowable. No row, so caller ID will not match it.
                continue
            if key in seen_for_candidate:
                # Two spellings of one number on the same person.
                continue
            seen_for_candidate.add(key)
            owner_key = (org_id, key)
            if owner_key in claimed:
                # Already owned by an earlier candidate; they keep it.
                continue
            claimed.add(owner_key)
            counter += 1
            pending.append(
                {
                    "id": f"cph_{counter:024x}",
                    "org_id": org_id,
                    "candidate_id": candidate_id,
                    "phone": key,
                    "position": position,
                }
            )
            position += 1
        if len(pending) >= _BATCH:
            _flush(bind, pending)
            pending = []
    _flush(bind, pending)


def _flush(bind, pending: list[dict]) -> None:
    if not pending:
        return
    bind.execute(
        sa.text(
            "INSERT INTO candidate_phones (id, org_id, candidate_id, phone, position) "
            "VALUES (:id, :org_id, :candidate_id, :phone, :position)"
        ),
        pending,
    )


def downgrade() -> None:
    op.drop_index("ix_candidate_phones_candidate", table_name="candidate_phones")
    op.drop_index("ix_candidate_phones_candidate_id", table_name="candidate_phones")
    op.drop_index("ix_candidate_phones_org_id", table_name="candidate_phones")
    op.drop_table("candidate_phones")

    op.drop_index("ix_candidate_preferences_org_review", table_name="candidate_preferences")
    op.drop_index("ix_candidate_preferences_org", table_name="candidate_preferences")
    op.drop_index("ix_candidate_preferences_source_call_id", table_name="candidate_preferences")
    op.drop_index("ix_candidate_preferences_candidate_id", table_name="candidate_preferences")
    op.drop_index("ix_candidate_preferences_org_id", table_name="candidate_preferences")
    op.drop_table("candidate_preferences")

    op.drop_index("ix_intake_calls_org_review", table_name="intake_calls")
    op.drop_index("ix_intake_calls_org_created", table_name="intake_calls")
    op.drop_index("ix_intake_calls_candidate_id", table_name="intake_calls")
    op.drop_index("ix_intake_calls_org_id", table_name="intake_calls")
    op.drop_table("intake_calls")
