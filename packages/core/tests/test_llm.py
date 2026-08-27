"""Gateway behaviour: request shape, retries, decoding, ledger, degradation.

Everything here runs on ``FakeTransport`` — no network, no key, no cost. The
live counterparts live in ``test_llm_live.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from juicebox.llm.client import (
    FakeTransport,
    LLMClient,
    LLMResult,
    LLMUnavailable,
    is_retryable,
    sdk_capability,
    set_llm,
)


class FlakyError(Exception):
    """Stands in for an SDK status error."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def build(responses=None, *, default=None, **kwargs) -> tuple[LLMClient, FakeTransport]:
    transport = FakeTransport(responses, default=default)
    client = LLMClient(transport=transport, enabled=True, model="test-model", **kwargs)
    return client, transport


class TestRequestShape:
    def test_json_call_sends_effort_and_schema(self):
        client, transport = build([{"ok": True}])
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

        client.json(system="sys", prompt="hello", schema=schema, effort="medium")

        sent = transport.calls[0]
        assert sent["model"] == "test-model"
        assert sent["output_config"]["effort"] == "medium"
        assert sent["output_config"]["format"] == {"type": "json_schema", "schema": schema}
        assert sent["messages"] == [{"role": "user", "content": "hello"}]

    def test_json_call_with_an_image_sends_a_multimodal_user_message(self):
        client, transport = build([{"type": "stop"}])
        png = b"\x89PNG\r\n\x1a\n"

        client.json(system="sys", prompt="what next", schema={}, image=png)

        content = transport.calls[0]["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "image"
        assert content[0]["source"]["media_type"] == "image/png"
        assert content[0]["source"]["data"]
        assert content[1] == {"type": "text", "text": "what next"}

    def test_text_call_sends_effort_without_a_format(self):
        """The 0.77.x trap: output_config carrying effort alone."""
        client, transport = build(["plain words"])

        result = client.text(system="sys", prompt="hello")

        assert result.text == "plain words"
        assert transport.calls[0]["output_config"] == {"effort": "low"}
        assert "format" not in transport.calls[0]["output_config"]


class TestDecoding:
    def test_json_is_parsed_onto_data(self):
        client, _ = build([{"job_titles": ["engineer"]}])
        result = client.json(system="s", prompt="p", schema={})
        assert result.data == {"job_titles": ["engineer"]}

    def test_non_json_body_raises_after_the_schemaless_retry(self):
        """Unreadable twice is a real failure; both attempts are scripted."""
        client, transport = build(["not json at all", "still not json"])
        with pytest.raises(LLMUnavailable, match="non-JSON"):
            client.json(system="s", prompt="p", schema={})
        assert len(transport.calls) == 2, "one retry, not a loop"

    def test_an_unreadable_schema_call_retries_without_the_schema(self):
        """The fix for glm-5.2 silently degrading every structured call.

        Schema-constrained output is a request, not a guarantee. Measured in
        production, roughly half of schema calls came back unreadable while the
        same prompt with no schema returned clean fenced JSON every time -- so
        every caller was falling back to heuristics for no reason.
        """
        client, transport = build(["not json at all", '{"ok": true}'])
        result = client.json(system="s", prompt="p", schema={"type": "object"})
        assert result.data == {"ok": True}
        assert len(transport.calls) == 2
        first, second = transport.calls
        assert "format" in first["output_config"], "the schema is tried first"
        assert "format" not in second["output_config"], "and dropped on the retry"

    def test_a_transport_failure_is_not_retried_without_the_schema(self):
        """Only unreadable output. A 429 says nothing about the schema."""
        from juicebox.llm.client import _is_unparseable

        assert _is_unparseable(LLMUnavailable("model returned non-JSON output: x"))
        assert not _is_unparseable(LLMUnavailable("model call failed: 429"))
        assert not _is_unparseable(LLMUnavailable("empty model response"))

    def test_empty_response_raises(self):
        client, _ = build([""])
        with pytest.raises(LLMUnavailable, match="empty"):
            client.json(system="s", prompt="p", schema={})

    def test_refusal_raises_and_is_not_retried(self):
        refusal = LLMResult(text="no", data=None, model="m", stop_reason="refusal")
        client, transport = build([refusal, {"ok": True}])
        with pytest.raises(LLMUnavailable, match="declined"):
            client.json(system="s", prompt="p", schema={})
        assert len(transport.calls) == 1


class TestRetries:
    def test_rate_limit_is_retried_then_succeeds(self):
        client, transport = build([FlakyError(429), {"ok": True}])
        result = client.json(system="s", prompt="p", schema={})
        assert result.data == {"ok": True}
        assert len(transport.calls) == 2
        assert client.usage()["retries"] == 1

    def test_server_error_is_retried(self):
        client, transport = build([FlakyError(503), FlakyError(500), {"ok": True}])
        result = client.json(system="s", prompt="p", schema={})
        assert result.data == {"ok": True}
        assert len(transport.calls) == 3

    def test_retries_are_bounded_then_surface_as_unavailable(self):
        client, transport = build([FlakyError(429)] * 5)
        with pytest.raises(LLMUnavailable):
            client.json(system="s", prompt="p", schema={})
        assert len(transport.calls) == client._max_retries + 1
        assert client.usage()["failures"] == 1

    def test_client_error_is_not_retried(self):
        client, transport = build([FlakyError(400), {"ok": True}])
        with pytest.raises(LLMUnavailable):
            client.json(system="s", prompt="p", schema={})
        assert len(transport.calls) == 1

    def test_retryable_classification(self):
        assert is_retryable(FlakyError(429))
        assert is_retryable(FlakyError(502))
        assert is_retryable(TimeoutError())
        assert not is_retryable(FlakyError(401))
        assert not is_retryable(LLMUnavailable("nope"))

    def test_async_path_retries_too(self):
        client, transport = build([FlakyError(429), {"ok": True}])
        result = asyncio.run(client.ajson(system="s", prompt="p", schema={}))
        assert result.data == {"ok": True}
        assert len(transport.calls) == 2


class TestLedger:
    def test_tokens_and_calls_accumulate(self):
        client, _ = build(default={"ok": True})
        client.json(system="s", prompt="one two three", schema={})
        client.json(system="s", prompt="four five", schema={})

        usage = client.usage()
        assert usage["calls"] == 2
        assert usage["input_tokens"] == 5
        assert usage["output_tokens"] > 0
        assert usage["by_model"]["test-model"]["calls"] == 2

    def test_failures_record_the_last_error(self):
        client, _ = build([FlakyError(400)])
        with pytest.raises(LLMUnavailable):
            client.json(system="s", prompt="p", schema={})
        usage = client.usage()
        assert usage["failures"] == 1
        assert "400" in usage["last_error"]


class TestAvailability:
    def test_no_api_key_is_unavailable(self):
        client = LLMClient(api_key="", enabled=True)
        assert client.available is False
        assert "ANTHROPIC_API_KEY" in client.unavailable_reason

    def test_disabled_flag_wins_over_a_key(self):
        client = LLMClient(api_key="sk-test", enabled=False)
        assert client.available is False
        assert "JUICEBOX_LLM_ENABLED" in client.unavailable_reason

    def test_calling_an_unavailable_client_raises(self):
        client = LLMClient(api_key="", enabled=True)
        with pytest.raises(LLMUnavailable, match="not configured"):
            client.json(system="s", prompt="p", schema={})

    def test_installed_sdk_meets_the_floor(self):
        """Guards the pin: >=0.78 is what makes output_config.effort legal."""
        ok, reason = sdk_capability()
        assert ok, reason


class TestProviderResolution:
    """Which backend a bare Settings resolves to, and what that makes available."""

    @staticmethod
    def _settings(monkeypatch, **env):
        from juicebox.config import get_settings, reset_settings_cache

        for name in (
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
            "JUICEBOX_LLM_PROVIDER",
            "JUICEBOX_LLM_MODEL",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        reset_settings_cache()
        return get_settings()

    def test_neither_key_resolves_to_none(self, monkeypatch):
        assert self._settings(monkeypatch).resolved_llm_provider == "none"

    def test_anthropic_key_alone_picks_anthropic(self, monkeypatch):
        settings = self._settings(monkeypatch, ANTHROPIC_API_KEY="sk-test")
        assert settings.resolved_llm_provider == "anthropic"
        assert settings.resolved_llm_model == "claude-opus-5"

    def test_openrouter_key_alone_picks_openrouter_and_its_model(self, monkeypatch):
        settings = self._settings(monkeypatch, OPENROUTER_API_KEY="or-test")
        assert settings.resolved_llm_provider == "openrouter"
        assert settings.resolved_llm_model == "z-ai/glm-5.3-flash"

    def test_both_keys_prefer_anthropic_so_dev_setups_are_unchanged(self, monkeypatch):
        settings = self._settings(
            monkeypatch, ANTHROPIC_API_KEY="sk-test", OPENROUTER_API_KEY="or-test"
        )
        assert settings.resolved_llm_provider == "anthropic"

    def test_an_explicit_provider_overrides_key_precedence(self, monkeypatch):
        settings = self._settings(
            monkeypatch,
            ANTHROPIC_API_KEY="sk-test",
            OPENROUTER_API_KEY="or-test",
            JUICEBOX_LLM_PROVIDER="openrouter",
        )
        assert settings.resolved_llm_provider == "openrouter"
        assert settings.resolved_llm_model == "z-ai/glm-5.3-flash"

    def test_model_override_wins_over_the_provider_default(self, monkeypatch):
        settings = self._settings(
            monkeypatch, OPENROUTER_API_KEY="or-test", JUICEBOX_LLM_MODEL="z-ai/glm-4.6"
        )
        assert settings.resolved_llm_model == "z-ai/glm-4.6"

    def test_unconfigured_client_names_both_env_vars(self, monkeypatch):
        self._settings(monkeypatch)
        client = LLMClient(enabled=True)
        assert client.available is False
        assert "ANTHROPIC_API_KEY" in client.unavailable_reason
        assert "OPENROUTER_API_KEY" in client.unavailable_reason

    def test_openrouter_does_not_need_the_anthropic_sdk_floor(self, monkeypatch):
        """An old anthropic install must not disable a working OpenRouter key."""
        self._settings(monkeypatch, OPENROUTER_API_KEY="or-test")
        monkeypatch.setattr(
            "juicebox.llm.client.sdk_capability", lambda: (False, "anthropic 0.40 is too old")
        )
        client = LLMClient(enabled=True)
        assert client.available is True
        assert client.provider == "openrouter"


class TestCallerDegradation:
    """The product must stay usable when the model does not."""

    def test_parser_falls_back_to_heuristics(self):
        from juicebox.search.parser import parse_query

        client, _ = build([FlakyError(500)] * 5)
        set_llm(client)
        try:
            parsed, parser_name = parse_query("senior python engineers in berlin")
        finally:
            set_llm(None)

        assert parser_name == "heuristic"
        assert "python" in parsed.must_have_skills
        assert client.usage()["failures"] == 1

    def test_parser_uses_the_model_when_it_works(self):
        from juicebox.search.parser import parse_query

        client, _ = build([{"job_titles": ["data scientist"], "must_have_skills": ["sql"]}])
        set_llm(client)
        try:
            parsed, parser_name = parse_query("anything at all")
        finally:
            set_llm(None)

        assert parser_name == "llm"
        assert parsed.job_titles == ["data scientist"]

    def test_reply_classification_falls_back(self):
        from juicebox.outreach.replies import classify_reply

        client, _ = build([FlakyError(500)] * 5)
        set_llm(client)
        try:
            verdict = classify_reply("Re: role", "Please unsubscribe me from this list.")
        finally:
            set_llm(None)

        assert verdict.intent == "unsubscribe"
        assert verdict.classified_by == "heuristic"


class TestOpenRouterImageParts:
    def test_anthropic_image_blocks_become_data_uris(self):
        from juicebox.llm.openrouter import _openrouter_messages

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "abc",
                        },
                    },
                    {"type": "text", "text": "what next"},
                ],
            }
        ]
        out = _openrouter_messages(messages)
        assert out[0]["content"][0] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc"},
        }
        assert out[0]["content"][1] == {"type": "text", "text": "what next"}
