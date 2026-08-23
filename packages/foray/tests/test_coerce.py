"""The three hazards coerce() exists to close.

Each test here is a live-bug pin, not a style check: before this module,
llm_parse spliced the model's ``metros`` and ``languages`` straight into SQL
predicates, so an unmappable spelling became a predicate matching zero rows and
a "%" became a full scan of 204,535 rows on four statements per request.
"""

from __future__ import annotations

from foray.api.coerce import MAX_ITEM_CHARS, MAX_LIST_ITEMS, coerce

# ------------------------------------------- hazard (a): unmappable values --


def test_unmappable_closed_vocabulary_values_are_dropped() -> None:
    """Dropped, not passed through. A predicate that cannot match reads to a
    recruiter as "nobody like this exists", which is the worse failure."""
    parsed = coerce(
        {
            "metros": ["San Francisco", "SF Bay Area", "Bengaluru", "Atlantis"],
            "languages": ["Golang", "Python", "COBOL"],
            "markers": ["ml", "not-a-marker-file"],
            "archetype_id": "no_such_archetype",
            "source": "carrier pigeon",
        }
    )
    # Both SF spellings collapse to the one key the column stores; the two
    # unsupported metros vanish rather than becoming dead predicates.
    assert parsed.metros == ["sf_bay_area"]
    assert parsed.languages == ["go", "python"]
    assert parsed.markers == ["ml"]
    assert parsed.archetype_id is None
    assert parsed.source is None


def test_valid_closed_vocabulary_values_survive() -> None:
    parsed = coerce({"archetype_id": "founding_ml", "source": "BOTH", "metros": ["nyc"]})
    assert parsed.archetype_id == "founding_ml"
    assert parsed.source == "both"
    assert parsed.metros == ["nyc"]  # canonical keys are idempotent


# ------------------------------------------------ hazard (b): unbounded work --


def test_list_and_item_lengths_are_capped() -> None:
    """Every text facet value becomes one OR arm, rebuilt for four
    statements."""
    parsed = coerce(
        {
            "titles": [f"title number {n}" for n in range(50)],
            "employers": ["x" * 500],
        }
    )
    assert len(parsed.titles) == MAX_LIST_ITEMS
    assert len(parsed.employers[0]) == MAX_ITEM_CHARS


def test_numeric_bounds_are_clamped_not_dropped() -> None:
    """A model saying 200 years meant "very senior"; clamping keeps a usable
    filter where dropping would silently widen the search."""
    parsed = coerce(
        {
            "experience_min": -5,
            "experience_max": 900,
            "seniority_min": 0,
            "seniority_max": 99,
            "tenure_min_months": -1,
            "tenure_max_months": 10_000,
        }
    )
    assert parsed.experience_min == 0.0 and parsed.experience_max == 50.0
    assert parsed.seniority_min == 1 and parsed.seniority_max == 6
    assert parsed.tenure_min_months == 0 and parsed.tenure_max_months == 600


def test_non_numeric_and_nonfinite_numbers_are_dropped() -> None:
    parsed = coerce(
        {"experience_min": "about five", "experience_max": float("nan"),
         "tenure_min_months": None}
    )
    assert parsed.experience_min is None
    assert parsed.experience_max is None
    assert parsed.tenure_min_months is None


def test_free_text_is_bounded() -> None:
    parsed = coerce({"free_text": "a " * 5000})
    assert len(parsed.free_text) <= 1000


# ------------------------------------------- hazard (c): wildcard injection --


def test_like_metacharacters_are_stripped_from_open_vocabularies() -> None:
    """``employers: ["%"]`` would become ``lower(s.employer) like '%%%'``:
    every row matches, the trigram index has no trigram to look up, and the
    facet becomes a full scan with no error message. These are bind params, so
    this is NOT SQL injection -- it is a self-inflicted DoS."""
    parsed = coerce(
        {
            "employers": ["%", "Goo%gle", "Str_pe"],
            "titles": ["_", "ML%Engineer"],
            "title_exclude": ["recruit%er"],
        }
    )
    assert parsed.employers == ["Google", "Strpe"]  # the bare "%" is gone
    assert parsed.titles == ["MLEngineer"]  # the bare "_" is gone
    assert parsed.title_exclude == ["recruiter"]
    for value in (*parsed.employers, *parsed.titles, *parsed.title_exclude):
        assert "%" not in value and "_" not in value and "\\" not in value


def test_trailing_backslash_cannot_reach_a_like_pattern() -> None:
    """LIKE's escape character: a trailing backslash makes the pattern
    malformed and asyncpg raises 22025, 500ing the whole search."""
    parsed = coerce({"employers": ["Acme\\"], "titles": ["\\"]})
    assert parsed.employers == ["Acme"]
    assert parsed.titles == []


# ----------------------------------------------------------- round-tripping --


def test_clean_filter_round_trips_unchanged() -> None:
    raw = {
        "archetype_id": "senior_backend",
        "metros": ["sf_bay_area", "nyc"],
        "languages": ["go", "rust"],
        "titles": ["backend engineer"],
        "title_exclude": ["recruiter"],
        "employers": ["Stripe"],
        "markers": ["infra"],
        "topics": ["kubernetes"],
        "experience_min": 5.0,
        "experience_max": 12.0,
        "tenure_min_months": 18,
        "tenure_max_months": 120,
        "seniority_min": 3,
        "seniority_max": 5,
        "is_manager": False,
        "is_founder": True,
        "source": "linkedin",
        "require_code_evidence": True,
        "free_text": "distributed storage",
    }
    parsed = coerce(dict(raw))
    for key, value in raw.items():
        assert getattr(parsed, key) == value, key
    # Idempotent: coercing the coerced dict changes nothing, which is what lets
    # both parsers exit through the same door.
    assert coerce(parsed.to_dict()) == parsed


def test_scalar_strings_are_accepted_as_single_item_lists() -> None:
    parsed = coerce({"titles": "ML engineer", "metros": "Palo Alto"})
    assert parsed.titles == ["ML engineer"]
    assert parsed.metros == ["sf_bay_area"]


def test_duplicate_items_collapse() -> None:
    parsed = coerce({"titles": ["ML Engineer", "ml engineer"], "metros": ["SF", "Oakland"]})
    assert parsed.titles == ["ML Engineer"]
    assert parsed.metros == ["sf_bay_area"]


def test_tri_state_flags_distinguish_false_from_unset() -> None:
    """``is_manager=False`` emits ``mgmt_rank is null``; unset emits nothing.
    Collapsing them would silently exclude every manager from every search."""
    assert coerce({}).is_manager is None
    assert coerce({"is_manager": False}).is_manager is False
    assert coerce({"is_manager": True}).is_manager is True


def test_policy_switches_default_to_their_documented_population_guard() -> None:
    """Defaults are per-facet, not uniform: each protects (or does not) the
    source population its column is missing."""
    parsed = coerce({})
    assert parsed.strict_language is False  # unknown language passes: 196,778 rows
    assert parsed.strict_topics is False  # unknown topics pass: same population
    assert parsed.strict_titles is False  # unknown title passes: 7,757 rows
    assert parsed.include_unknown_seniority is True  # NULL rank is most of the corpus
    assert parsed.include_unknown_tenure is False  # numeric band, strict like experience
    assert parsed.include_unknown_experience is False
