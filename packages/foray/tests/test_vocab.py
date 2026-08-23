"""Language vocabulary: the two-spelling problem and the \\b-after-'+' trap.

Every case here is a regression pin for a bug that shipped: a recruiter typing
"c++" produced ``primary_language = any('{cpp}')``, which matches zero rows
because derive stores "c++"; and the old ``rf"\\b{lang}\\b"`` template could
never have matched "c++" anyway.
"""

from __future__ import annotations

import pytest

from foray.api.parse import heuristic_parse
from foray.harvest.shards import LANGUAGES
from foray.vocab import (
    CANONICAL_LANGUAGES,
    GITHUB_SEARCH_LANGUAGES,
    LANGUAGE_ALIASES,
    match_languages,
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
