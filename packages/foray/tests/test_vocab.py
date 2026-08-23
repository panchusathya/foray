"""Query vocabularies: the two-spelling problem, and the collapse rules.

Every case here is a regression pin for a bug that shipped: a recruiter typing
"c++" produced ``primary_language = any('{cpp}')``, which matches zero rows
because derive stores "c++"; and the old ``rf"\\b{lang}\\b"`` template could
never have matched "c++" anyway. The metro half pins the same class of bug one
column over -- a location spelling the corpus does not store.
"""

from __future__ import annotations

import pytest

from foray.api.parse import heuristic_parse
from foray.harvest.shards import LANGUAGES
from foray.vocab import (
    CANONICAL_LANGUAGES,
    GITHUB_SEARCH_LANGUAGES,
    LANGUAGE_ALIASES,
    canonical_employer,
    canonical_metro,
    match_languages,
    metro_alias_table,
    metro_keys,
    metro_like_patterns,
    metro_spans,
    vocab_hash,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("senior c++ engineer", ["c++"]),
        ("cpp developer", ["c++"]),           # alias -> stored form
        ("golang backend", ["go"]),
        ("c# and typescript", ["c#", "typescript"]),
        ("looking for a py dev", ["python"]),
        ("embedded c firmware", ["c"]),        # bare c IS a language here
        ("rust and go systems", ["rust", "go"]),
    ],
)
def test_match_languages(text: str, expected: list[str]) -> None:
    assert match_languages(text) == expected


def test_cpp_does_not_emit_bare_c() -> None:
    """The C-family boundary cases. A bogus bare "c" alongside "c++" would
    widen the SQL filter to every C row in the corpus."""
    assert match_languages("c++ dev") == ["c++"]
    assert match_languages("c# only") == ["c#"]
    assert match_languages("c sharp developer") == ["c#"]
    assert match_languages("c plus plus engineer") == ["c++"]


def test_substring_languages_do_not_bleed() -> None:
    assert match_languages("java developer") == ["java"]
    assert match_languages("javascript developer") == ["javascript"]
    # "java" must not fire from inside "javascript"
    assert "java" not in match_languages("javascript only")


def test_alias_and_canonical_dedupe_to_one_stored_form() -> None:
    assert match_languages("cpp / c++ developer") == ["c++"]


def test_appearance_order_not_canonical_order() -> None:
    # typescript precedes python in the text but not in CANONICAL_LANGUAGES.
    assert match_languages("typescript then python") == ["typescript", "python"]


def test_no_match_is_empty() -> None:
    assert match_languages("founding engineer, shipped training infra") == []
    assert match_languages("") == []


def test_every_alias_target_is_canonical() -> None:
    """An alias pointing at a non-stored form would silently produce a
    zero-match SQL predicate — the exact class of bug this module exists to
    kill."""
    assert set(LANGUAGE_ALIASES.values()) <= set(CANONICAL_LANGUAGES)


def test_shard_grid_spelling_and_size_are_unchanged() -> None:
    """harvest needs GitHub search-qualifier spellings ('cpp', not 'c++'), and
    test_harvest pins the grid size — vocab must not silently regrow it."""
    assert "cpp" in GITHUB_SEARCH_LANGUAGES
    assert "c++" not in GITHUB_SEARCH_LANGUAGES
    assert len(GITHUB_SEARCH_LANGUAGES) == 12
    assert list(GITHUB_SEARCH_LANGUAGES) == LANGUAGES  # deprecated re-export


def test_heuristic_parse_extracts_cpp_as_stored_form() -> None:
    """End to end: the query path must emit what the column holds."""
    assert heuristic_parse("c++ systems engineer").languages == ["c++"]


# ---------------------------------------------------------------------- metros --


@pytest.mark.parametrize(
    "raw",
    [
        "San Francisco",
        "SF",
        "SF Bay Area",
        "San Francisco Bay Area",
        "South San Francisco",
        "Palo Alto",
        "Mountain View",
        "Oakland",
        "Berkeley",
        "San Jose",
        "Sunnyvale",
        "Santa Clara",
        "Redwood City",
        "Menlo Park",
        # Real shapes from the corpus: a state suffix, a "Greater ... Area"
        # wrapper, and punctuation between tokens.
        "San Francisco, CA",
        "SF, CA",
        "Greater San Francisco Bay Area",
        "san-francisco",
    ],
)
def test_the_whole_bay_area_collapses_to_one_key(raw: str) -> None:
    """The point of the file: 18 spellings, one predicate. metros.txt keeps
    these as separate SHARDS on purpose, which is why it is the wrong table."""
    assert canonical_metro(raw) == "sf_bay_area"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("New York", "nyc"),
        ("NYC", "nyc"),
        ("Brooklyn", "nyc"),
        ("Seattle", "seattle"),
        ("Bellevue", "seattle"),
        ("Redmond", "seattle"),
        ("Austin", "austin"),
        ("Boston", "boston"),
        ("Cambridge", "boston"),
        ("Los Angeles", "la"),
        ("LA", "la"),
        ("Santa Monica", "la"),
        ("Washington, D.C.", "dc"),
    ],
)
def test_required_metro_keys(raw: str, expected: str) -> None:
    assert canonical_metro(raw) == expected


def test_unmappable_locations_return_none_rather_than_a_guess() -> None:
    """None is what makes coerce DROP the facet. A guess here would be a
    permanently wrong location_metro on a row we only derive once."""
    for raw in ("Bengaluru", "Surfside, FL", "remote", "", "   ", None):
        assert canonical_metro(raw) is None


def test_canonical_metro_is_idempotent_on_its_own_keys() -> None:
    """derive re-normalizes an already-normalized column and coerce
    re-validates an already-parsed filter; both would eat their own output
    otherwise."""
    for key in metro_keys():
        assert canonical_metro(key) == key


def test_short_aliases_never_match_by_containment_on_a_stored_field() -> None:
    """"la" inside "La Jolla" and "sf" inside "Surfside" would mis-file rows
    permanently. Long aliases still win by containment, which is how "La
    Jolla" reaches San Diego at all."""
    assert canonical_metro("La Jolla") == "san_diego"
    assert canonical_metro("Surfside") is None
    assert canonical_metro("LA") == "la"
    assert canonical_metro("LA, CA") == "la"


def test_metro_spans_are_looser_than_canonical_metro_and_report_positions() -> None:
    """Query side: "in SF" must resolve even though "sf" is containment-unsafe
    on a stored field. The spans exist so heuristic_parse can REMOVE what it
    consumed from free_text."""
    spans = metro_spans("senior backend engineer in sf and nyc")
    assert [key for _s, _e, key in spans] == ["sf_bay_area", "nyc"]
    for start, end, _key in spans:
        assert 0 <= start < end


def test_metro_spans_do_not_overlap_and_prefer_the_longest_alias() -> None:
    spans = metro_spans("hiring in la jolla")
    assert [key for _s, _e, key in spans] == ["san_diego"]


def test_metro_like_patterns_are_trigram_usable_and_minimal() -> None:
    """Two prunings, both about the index: a pattern under 3 characters has no
    trigram to look up (seq scan), and an alias containing another alias adds
    an index scan that can return no extra rows."""
    patterns = metro_like_patterns("sf_bay_area")
    assert all(len(p) >= 3 for p in patterns)
    assert "san francisco" in patterns
    # "south san francisco" and "sf bay area" are covered by shorter aliases.
    assert "south san francisco" not in patterns
    assert "sf bay area" not in patterns
    assert "sf" not in patterns
    # "la" is 2 chars: it resolves to a key, it never becomes a LIKE body.
    assert "la" not in metro_like_patterns("la")
    assert metro_like_patterns("no_such_key") == ()


def test_every_metro_alias_round_trips_to_its_own_key() -> None:
    """An alias that resolves to a DIFFERENT key is a silent mis-file: the
    facet still works, it just returns the wrong city."""
    for key, aliases in metro_alias_table().items():
        for alias in aliases:
            assert canonical_metro(alias) == key, (alias, key)


# ------------------------------------------------------------------- employers --


def test_canonical_employer_resolves_only_what_the_alias_table_knows() -> None:
    """The equality fast path is only safe for spellings derive normalizes.
    Everything else must come back None so the caller uses a trigram LIKE --
    "UCSF" and "University of California, San Francisco" are separate
    employers in the corpus today and no table entry makes them one."""
    assert canonical_employer("google") == "google"
    assert canonical_employer("@Alphabet") == "google"
    assert canonical_employer("  fb ") == "meta"
    assert canonical_employer("UCSF") is None
    assert canonical_employer("University of California, San Francisco") is None
    assert canonical_employer("") is None
    assert canonical_employer(None) is None


# ------------------------------------------------------------------ vocab hash --


def test_vocab_hash_is_stable_and_covers_the_alias_files() -> None:
    """Folded into the parse cache key: an alias-file edit must make cached
    parses unreachable, or a recruiter keeps getting yesterday's vocabulary."""
    first = vocab_hash()
    assert len(first) == 64 and first == vocab_hash()

    table = dict(metro_alias_table())
    table["atlantis"] = ("atlantis",)
    metro_alias_table.cache_clear()
    vocab_hash.cache_clear()
    try:
        import foray.vocab as vocab_module

        original = vocab_module.metro_alias_table
        vocab_module.metro_alias_table = lambda: table  # type: ignore[assignment]
        assert vocab_hash() != first
    finally:
        vocab_module.metro_alias_table = original  # type: ignore[assignment]
        vocab_hash.cache_clear()
        metro_alias_table.cache_clear()
    assert vocab_hash() == first
