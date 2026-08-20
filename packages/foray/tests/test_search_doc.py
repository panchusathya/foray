from __future__ import annotations

import pytest

from foray.search_doc import RANK_WEIGHTS, TS_CONFIG, build_websearch_or


def test_config_is_english() -> None:
    assert TS_CONFIG == "english"
    assert RANK_WEIGHTS.startswith("{")


def test_or_builder_drops_noise_and_digits() -> None:
    q = build_websearch_or("senior backend engineer in Go with 5 years")
    assert q is not None
    assert "engineer" not in q
    assert "senior" not in q
    assert "5" not in q.split()
    assert "go" in q
    assert " or " in q


def test_quoted_phrases_survive() -> None:
    q = build_websearch_or('"vision language" engineer trained vlm')
    assert q is not None
    assert '"vision language"' in q
    assert "vlm" in q


def test_negation_tokens_are_stripped() -> None:
    q = build_websearch_or("backend go --java -rust")
    assert q is not None
    assert "java" not in q
    assert "rust" not in q
    assert "go" in q


def test_empty_and_noise_only_return_none() -> None:
    assert build_websearch_or("") is None
    assert build_websearch_or("   ") is None
    assert build_websearch_or("senior engineer with years of experience") is None


def test_cplusplus_is_quoted() -> None:
    q = build_websearch_or("systems engineer in c++")
    assert q is not None
    assert '"c++"' in q


@pytest.mark.parametrize(
    "raw",
    [
        "a & b | c",
        "foo!",
        "bar:",
        "(unbalanced",
        "a <-> b",
        "term:*",
        '"' * 3,
        "x" * 10_000,
        "emoji 🚀 vlm",
        "::: ***",
    ],
)
def test_adversarial_input_never_raises(raw: str) -> None:
    build_websearch_or(raw)  # must not raise; None is fine
