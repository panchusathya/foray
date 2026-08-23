"""The recency predicate, in one place, with explicit three-valued semantics.

WHY A MODULE FOR ONE STRING. The old predicate,
``s.activity_at > now() - interval 'N months'``, was copy-pasted into the search
filter and into all 15 archetype gates (at 18 or 24 months, varying with no
recorded reason). It was also a **no-op**: ``activity_at`` for LinkedIn rows was
the import wall-clock, so 196,778 / 196,778 LinkedIn rows passed the 24-month
floor, all stamped ``activity_kind='snapshot'`` inside one month. 16 copies of a
dead clause is 16 places to get the fix wrong, hence one function.

THE THREE-VALUED SEMANTICS. Once ``activity_at`` is evidence-or-NULL
(``ingest/activity.py`` decodes real feed dates for ~48.7% of LinkedIn records),
"recent" has three states, not two:

  * **known and in-window** -> passes. Real, dated activity evidence.
  * **known and out-of-window** -> fails. This is the arm that finally does
    work: before, no LinkedIn row could ever land here.
  * **UNKNOWN** (``activity_at is null``) -> passes *only while the snapshot is
    fresh*, i.e. ``profile_asof`` is inside ``snapshot_months``.

Both arms are load-bearing, and each alone is a known failure mode:

  * **First arm only** would silently delete the ~51% of LinkedIn records with
    no decodable activity id. A row with no evidence of *when* is not a row
    with evidence of *stale* — dropping it is a data-loss bug dressed up as a
    quality filter.
  * **Second arm only** puts us straight back on the import clock, which is the
    196,778/196,778 no-op we are removing.

So: unknown activity is admitted on the strength of the snapshot, and
``snapshot_months`` bounds how long that credit lasts.

Deliberately dependency-free — no ``foray.config`` import (see
``.importlinter``: the pure SQL/ranking layer must not reach into query-path
infrastructure). The caller passes the two numbers.
"""

from __future__ import annotations


def recency_clause(months: int, snapshot_months: int) -> str:
    """SQL boolean over candidate_signals (alias ``s``). See module docstring.

    Both bounds are interpolated as SQL **literals**, not bind parameters,
    because this string is embedded in archetype gates and in count/filter SQL
    that must stay ``$N``-numbering-compatible with its callers. They are
    config values, never user input — and ``int()`` is the injection guard, the
    same practice as the activity-floor line this replaced in
    ``api/search.py._filter_clauses`` and as the gate interpolation there.
    """
    months = int(months)
    snapshot_months = int(snapshot_months)
    return (
        f"(s.activity_at > now() - interval '{months} months'"
        f" or (s.activity_at is null"
        f" and s.profile_asof > now() - interval '{snapshot_months} months'))"
    )
