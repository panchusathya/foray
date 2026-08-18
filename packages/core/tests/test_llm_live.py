"""Contract tests against the real provider APIs.

Marked ``live`` and excluded from the default run by ``addopts`` in
pyproject.toml, so a missing key is never a red build:

    make test-live            # or: pytest -m live

These assert the *wire contract* — that the request shape we build is accepted
and the response can be decoded — not model wording, which is not stable enough
to assert on. The first test here is the one that would have caught the
``anthropic>=0.40`` floor: it is the only check that proves an
``output_config`` carrying ``effort`` is accepted by the live API.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
VOYAGE_KEY = os.environ.get("VOYAGE_API_KEY", "")

needs_anthropic = pytest.mark.skipif(not ANTHROPIC_KEY, reason="ANTHROPIC_API_KEY is not set")
needs_voyage = pytest.mark.skipif(not VOYAGE_KEY, reason="VOYAGE_API_KEY is not set")


@pytest.fixture
def live_env(monkeypatch):
    """Undo the offline defaults the autouse fixture applies."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("JUICEBOX_LLM_ENABLED", "true")
    if VOYAGE_KEY:
        monkeypatch.setenv("VOYAGE_API_KEY", VOYAGE_KEY)

    from juicebox.config import reset_settings_cache
    from juicebox.llm.client import reset_llm
    from juicebox.llm.embeddings import reset_embedder

    reset_settings_cache()
    reset_llm()
    reset_embedder()
    yield
    reset_settings_cache()
    reset_llm()
    reset_embedder()


@needs_anthropic
class TestAnthropicContract:
    def test_output_config_with_effort_and_schema_is_accepted(self, live_env):
        """The exact request shape juicebox builds must be legal on the live API."""
        from juicebox.llm.client import get_llm

        llm = get_llm()
        assert llm.available, llm.unavailable_reason

        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["city", "country"],
            "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
        }
        result = llm.json(
            system="You extract locations. Reply only with the requested JSON.",
            prompt="Where is the Eiffel Tower? Answer with city and country.",
            schema=schema,
            max_tokens=256,
        )

        assert set(result.data) == {"city", "country"}
        assert "paris" in result.data["city"].lower()
        assert result.input_tokens > 0 and result.output_tokens > 0

    def test_configured_model_is_callable(self, live_env):
        """Catches JUICEBOX_LLM_MODEL naming a model this account cannot use."""
        from juicebox.config import get_settings
        from juicebox.llm.client import get_llm

        settings = get_settings()
        result = get_llm().text(
            system="Reply with exactly one word.",
            prompt="Say the word: ready",
            max_tokens=32,
        )
        assert result.text
        assert result.model, "the API did not echo a model name back"
        assert settings.llm_model.split("-")[0] in result.model

    def test_text_call_without_a_schema_is_accepted(self, live_env):
        """output_config containing only `effort` — illegal before SDK 0.78."""
        from juicebox.llm.client import get_llm

        result = get_llm().text(system="Be terse.", prompt="Name one primary colour.", max_tokens=32)
        assert result.text.strip()

    def test_query_parser_returns_usable_structure(self, live_env):
        from juicebox.search.parser import parse_query

        parsed, parser_name = parse_query(
            "senior machine learning engineers in San Francisco with pytorch, 5+ years"
        )

        assert parser_name == "llm", "expected the model parser to be used"
        assert "pytorch" in parsed.must_have_skills + parsed.nice_to_have_skills
        assert "senior" in parsed.seniority
        assert parsed.search_text

    def test_ledger_counts_real_spend(self, live_env):
        from juicebox.llm.client import get_llm

        llm = get_llm()
        llm.text(system="Be terse.", prompt="Say hi.", max_tokens=16)
        usage = llm.usage()

        assert usage["calls"] >= 1
        assert usage["input_tokens"] > 0
        assert usage["by_model"]


@needs_voyage
class TestVoyageContract:
    def test_embeddings_are_normalised_vectors(self, live_env):
        import numpy as np

        from juicebox.config import get_settings
        from juicebox.llm.embeddings import VoyageEmbedder

        settings = get_settings()
        embedder = VoyageEmbedder(VOYAGE_KEY, settings.voyage_model, settings.embedding_dim)
        matrix = embedder.embed(["senior machine learning engineer", "frontend designer"])

        assert matrix.shape[0] == 2
        assert matrix.shape[1] > 0
        norms = np.linalg.norm(matrix, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-3), f"not L2 normalised: {norms}"

    def test_a_bad_key_degrades_instead_of_raising(self, live_env):
        """The fallback in VoyageEmbedder.embed must actually catch real failures."""
        from juicebox.llm.embeddings import VoyageEmbedder

        embedder = VoyageEmbedder("definitely-not-a-real-key", "voyage-3", 512)
        matrix = embedder.embed(["anything"])

        assert matrix.shape == (1, 512), "expected the hashing fallback shape"
