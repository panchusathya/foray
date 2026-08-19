"""OpenRouter transport: request translation, response mapping, error classes.

The translation functions are pure, so they are fed the exact dict that
``LLMClient._build_kwargs`` produces — if that shape changes, these fail rather
than silently sending a malformed body. The one end-to-end case runs over
``httpx.MockTransport``: no network, no API key.
"""

from __future__ import annotations

import httpx
import pytest

from juicebox.llm.client import LLMClient, LLMUnavailable, is_retryable, loose_json
from juicebox.llm.openrouter import (
    LLMTransportError,
    OpenRouterTransport,
    estimated_cost_usd,
    from_openrouter,
    to_openrouter,
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def build_kwargs(*, schema=None, effort="low"):
    """The internal request shape, built by the client itself."""
    return LLMClient._build_kwargs(
        model="z-ai/glm-5.2",
        system="You are terse.",
        prompt="Say hi.",
        schema=schema,
        effort=effort,
        max_tokens=512,
    )


class TestRequestTranslation:
    def test_system_becomes_the_first_message(self):
        body = to_openrouter(build_kwargs())
        assert body["messages"][0] == {"role": "system", "content": "You are terse."}
        assert body["messages"][1]["content"] == "Say hi."
        assert "system" not in body

    def test_effort_maps_to_reasoning(self):
        assert to_openrouter(build_kwargs(effort="medium"))["reasoning"] == {"effort": "medium"}

    def test_unknown_effort_falls_back_to_low(self):
        kwargs = build_kwargs()
        kwargs["output_config"]["effort"] = "ludicrous"
        assert to_openrouter(kwargs)["reasoning"] == {"effort": "low"}

    def test_schema_becomes_response_format_and_pins_the_provider(self):
        body = to_openrouter(build_kwargs(schema=SCHEMA))
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"] is SCHEMA
        # A provider that ignores response_format would answer in prose.
        assert body["provider"] == {"require_parameters": True}

    def test_strict_only_when_the_schema_closes_the_root(self):
        assert to_openrouter(build_kwargs(schema=SCHEMA))["response_format"]["json_schema"]["strict"]
        open_schema = {"type": "object", "properties": {}}
        body = to_openrouter(build_kwargs(schema=open_schema))
        assert body["response_format"]["json_schema"]["strict"] is False

    def test_text_calls_carry_no_response_format(self):
        body = to_openrouter(build_kwargs())
        assert "response_format" not in body
        assert "provider" not in body

    def test_token_budget_and_usage_accounting_survive(self):
        body = to_openrouter(build_kwargs())
        assert body["max_tokens"] == 512
        assert body["usage"] == {"include": True}


class TestResponseMapping:
    def _payload(self, **overrides):
        payload = {
            "model": "z-ai/glm-5.2",
            "choices": [{"message": {"content": '{"answer": "hi"}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        }
        payload.update(overrides)
        return payload

    def test_maps_text_tokens_and_model(self):
        result = from_openrouter(self._payload())
        assert result.text == '{"answer": "hi"}'
        assert (result.input_tokens, result.output_tokens) == (11, 3)
        assert result.model == "z-ai/glm-5.2"

    @pytest.mark.parametrize(
        "finish,expected",
        [("stop", "end_turn"), ("length", "max_tokens"), ("content_filter", "refusal")],
    )
    def test_finish_reasons_map_to_the_internal_vocabulary(self, finish, expected):
        payload = self._payload()
        payload["choices"][0]["finish_reason"] = finish
        assert from_openrouter(payload).stop_reason == expected

    def test_no_choices_raises_rather_than_returning_empty(self):
        with pytest.raises(LLMTransportError):
            from_openrouter({"choices": [], "error": {"message": "upstream is down"}})


class TestErrorClassification:
    """A 429 must be retried. httpx keeps status on .response, which the
    client's duck-typed check cannot see — hence the wrapper."""

    def _transport(self, handler):
        return OpenRouterTransport(
            api_key="test-key",
            base_url="https://openrouter.test/api/v1",
            timeout=5.0,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_rate_limit_is_retryable(self):
        transport = self._transport(lambda request: httpx.Response(429, text="slow down"))
        with pytest.raises(LLMTransportError) as caught:
            transport.complete(build_kwargs())
        assert caught.value.status_code == 429
        assert is_retryable(caught.value) is True

    def test_server_error_is_retryable(self):
        transport = self._transport(lambda request: httpx.Response(503, text="unavailable"))
        with pytest.raises(LLMTransportError) as caught:
            transport.complete(build_kwargs())
        assert is_retryable(caught.value) is True

    def test_bad_request_is_not_retryable(self):
        transport = self._transport(lambda request: httpx.Response(400, text="bad schema"))
        with pytest.raises(LLMTransportError) as caught:
            transport.complete(build_kwargs())
        assert is_retryable(caught.value) is False

    def test_timeout_becomes_a_retryable_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("too slow", request=request)

        with pytest.raises(TimeoutError) as caught:
            self._transport(handler).complete(build_kwargs())
        assert is_retryable(caught.value) is True


class TestEndToEnd:
    def test_a_structured_call_round_trips_over_a_mock_transport(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "model": "z-ai/glm-5.2",
                    "choices": [
                        {"message": {"content": '{"answer": "42"}'}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "cost": 0.000123},
                },
            )

        transport = OpenRouterTransport(
            api_key="test-key",
            base_url="https://openrouter.test/api/v1",
            timeout=5.0,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        result = transport.complete(build_kwargs(schema=SCHEMA))

        assert result.text == '{"answer": "42"}'
        assert seen["headers"]["authorization"] == "Bearer test-key"
        assert seen["headers"]["x-title"]  # attribution, per OpenRouter guidance
        assert seen["body"]["model"] == "z-ai/glm-5.2"
        # The provider reported a cost, so we bill that rather than an estimate.
        assert transport.last_cost_usd == pytest.approx(0.000123)

    def test_the_client_uses_the_transport_and_tallies_cost(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "z-ai/glm-5.2",
                    "choices": [
                        {"message": {"content": '{"answer": "ok"}'}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
                },
            )

        transport = OpenRouterTransport(
            api_key="test-key",
            base_url="https://openrouter.test/api/v1",
            timeout=5.0,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        client = LLMClient(transport=transport, enabled=True, model="z-ai/glm-5.2")
        client._provider = "openrouter"  # what config resolution would set
        result = client.json(system="s", prompt="p", schema=SCHEMA)

        assert result.data == {"answer": "ok"}
        # No reported cost, so the published table applies: $0.50 + $3.15.
        assert client.ledger.snapshot()["cost_usd"] == pytest.approx(3.65)


class TestCostTable:
    def test_known_model_prices_a_million_tokens_each_way(self):
        assert estimated_cost_usd("z-ai/glm-5.2", 1_000_000, 1_000_000) == pytest.approx(3.65)

    def test_unknown_model_costs_zero_rather_than_guessing(self):
        assert estimated_cost_usd("who/knows", 1_000_000, 1_000_000) == 0.0


class TestTolerantDecode:
    """Not every provider honours response_format as strictly as the SDK does."""

    def test_plain_json_is_unchanged(self):
        assert loose_json('{"a": 1}') == {"a": 1}

    def test_fenced_json_parses(self):
        assert loose_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_unlabelled_fence_parses(self):
        assert loose_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_wrapped_object_parses(self):
        assert loose_json('Sure! Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}

    def test_array_payload_parses(self):
        assert loose_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]

    def test_unparseable_output_raises(self):
        with pytest.raises(ValueError):
            loose_json("I would rather not.")

    def test_the_client_surfaces_bad_json_as_unavailable(self):
        from juicebox.llm.client import FakeTransport

        client = LLMClient(transport=FakeTransport(["not json at all"]), enabled=True)
        with pytest.raises(LLMUnavailable):
            client.json(system="s", prompt="p", schema=SCHEMA)
