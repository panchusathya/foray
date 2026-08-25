"""Drop caller-ID sentinels from the candidate phone projection.

``intake.phones.lookup_key`` used to accept any ``+``-prefixed 8-15 digit
string, so a withheld-caller-ID placeholder -- ``+266696687``, the keypad
spelling of ANONYMOUS -- was a well-formed key and could be stored as a
person's number. Every subsequent caller who withheld their number then
resolved to that person. ``lookup_key`` now refuses these, but a row written
before the fix would still match, so the stored ones have to go.

Deleting here is safe and is why the projection exists: ``candidate_phones`` is
a derived subset of ``Candidate.phones``, rebuilt by
``repository.sync_candidate_phones``. The JSON keeps whatever was imported; only
the lookup path stops honouring it. That also makes this irreversible in the
only direction that matters -- ``downgrade`` deliberately does not put the rows
back, because restoring them would restore the mismatch.

Written as a digit-normalising scan in Python rather than a ``LIKE`` sweep so it
agrees exactly with ``is_sentinel_number``, including the leading-region-code
form and the repeated-digit case, on both SQLite and Postgres.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a1c73f4b28"
down_revision: str | None = "b3f7a06d94c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Imported rather than re-listed: one definition of "this is not a number",
    # so a sentinel added later cannot be fixed in the code and left behind in
    # the data.
    from juicebox.intake.phones import is_sentinel_number

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, phone FROM candidate_phones")).fetchall()
    doomed = [
        row.id
        for row in rows
        if is_sentinel_number("".join(ch for ch in (row.phone or "") if ch.isdigit()))
    ]
    if not doomed:
        return
    # Chunked so a workspace with a large projection does not build one
    # statement with tens of thousands of bind parameters.
    for start in range(0, len(doomed), 500):
        chunk = doomed[start : start + 500]
        bind.execute(
            sa.text("DELETE FROM candidate_phones WHERE id IN :ids").bindparams(
                sa.bindparam("ids", value=tuple(chunk), expanding=True)
            )
        )


def downgrade() -> None:
    """Deliberately empty: see the module docstring."""
