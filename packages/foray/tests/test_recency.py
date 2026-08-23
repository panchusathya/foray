from __future__ import annotations

from foray.recency import recency_clause


def test_exact_sql_shape_is_pinned():
    """Pinned exactly: this one string is the recency predicate for the search
    filter AND for all 15 archetype gates, which used to hold 15 hand-copied
    literals at 18-or-24 months. A drift here does not error, it silently
    changes which corpus is searchable."""
    assert recency_clause(24, 18) == (
        "(s.activity_at > now() - interval '24 months'"
        " or (s.activity_at is null"
        " and s.profile_asof > now() - interval '18 months'))"
    )


def test_both_arms_are_present():
    clause = recency_clause(24, 24)
    # Arm 1: real, dated activity inside the window.
    assert "s.activity_at > now() - interval '24 months'" in clause
    # Arm 2: UNKNOWN activity, admitted only while the snapshot is fresh.
    # Without it, the ~51% of LinkedIn records with no decodable activity id
    # vanish; with only it, we are back on the import clock that let
    # 196,778/196,778 rows pass.
    assert "s.activity_at is null" in clause
    assert "s.profile_asof > now() - interval '24 months'" in clause
    assert " or " in clause


def test_int_coercion_guards_the_literal_interpolation():
    """Both bounds are SQL literals, not bind params (the clause is embedded in
    archetype gates that must stay $N-numbering-compatible), so int() is the
    injection guard."""
    assert recency_clause("36", 12.9) == recency_clause(36, 12)
    assert "'36 months'" in recency_clause("36", 12.9)
    assert "'12 months'" in recency_clause("36", 12.9)
    # A string carrying SQL cannot survive int().
    try:
        recency_clause("24; drop table candidates", 24)
    except ValueError:
        pass
    else:  # pragma: no cover - the guard is the point of the test
        raise AssertionError("int() must reject a non-numeric bound")


def test_clause_is_parenthesised_for_and_chaining():
    # It is joined into a WHERE with `and`, and its own top-level `or` would
    # otherwise swallow every following predicate.
    clause = recency_clause(24, 24)
    assert clause.startswith("(") and clause.endswith(")")


def test_module_is_dependency_free():
    """No settings import: the caller passes numbers. Enforced in .importlinter
    too, asserted here so the reason is visible where the code is."""
    import foray.recency as mod

    assert not hasattr(mod, "settings")
