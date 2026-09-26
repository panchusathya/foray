"""Job discovery turns: plan/ask, transient search, already_have, no-key skip."""

from __future__ import annotations

from juicebox.jobmatch.exa import ExaClient, set_exa
from juicebox.llm.client import FakeTransport, LLMClient, set_llm


def _ask_plan(**overrides):
    body = {
        "action": "ask",
        "questions": ["Which city or remote?", "What seniority?"],
        "queries": [],
        "assumptions": [],
        "reply": "A couple of details before I search.",
    }
    body.update(overrides)
    return body


def _search_plan(queries=None, **overrides):
    body = {
        "action": "search",
        "questions": [],
        "queries": queries or ["staff machine learning engineer San Francisco"],
        "assumptions": ["San Francisco from the brief."],
        "reply": "Searching now.",
    }
    body.update(overrides)
    return body


def _summary(**overrides):
    body = {"reply": "2 postings below.", "follow_up": "Want me to widen to remote?"}
    body.update(overrides)
    return body


def _exa_hits():
    return [
        {
            "id": "exa-1",
            "url": "https://jobs.example.com/ml-discover",
            "title": "Staff Machine Learning Engineer",
            "text": "Python and ranking in production.",
            "author": "Datadog",
        },
        {
            "id": "exa-2",
            "url": "https://jobs.example.com/infra-discover",
            "title": "AI Infra Engineer",
            "text": "CUDA and Kubernetes.",
            "author": "Anthropic",
        },
    ]


def _exa_transport(calls):
    def transport(_method, url, payload):
        calls.append({"url": url, "payload": payload})
        if "contents" in url:
            return {"results": []}
        return {"results": _exa_hits()}

    return transport


def _turn(client, account, messages, **extra):
    return client.post(
        "/v1/job-discovery/turns",
        json={"messages": messages, **extra},
        headers=account["headers"],
    )


def _enable_llm(responses):
    set_llm(LLMClient(transport=FakeTransport(responses), enabled=True, model="test-model"))


class TestDiscoverAsk:
    def test_underspecified_brief_asks_and_does_not_call_exa(self, client, account):
        calls = []
        set_exa(ExaClient(api_key="test", transport=_exa_transport(calls)))
        _enable_llm([_ask_plan()])
        response = _turn(
            client, account, [{"role": "user", "content": "find me AI infra jobs"}]
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["questions"]
        assert body["results"] == []
        assert body["steps"] == []
        assert calls == []
        assert client.get("/v1/job-postings", headers=account["headers"]).json()["total"] == 0


class TestDiscoverSearch:
    def test_queries_capped_at_three(self, client, account):
        calls = []
        set_exa(ExaClient(api_key="test", transport=_exa_transport(calls)))
        _enable_llm(
            [
                _search_plan(queries=["q1", "q2", "q3", "q4", "q5"]),
                _summary(),
            ]
        )
        _turn(client, account, [{"role": "user", "content": "Staff ML in SF"}])
        search_calls = [c for c in calls if "search" in c["url"]]
        assert len(search_calls) == 3


class TestDiscoverDegrade:
    def test_no_exa_key_is_200_with_skip_step(self, client, account):
        set_exa(ExaClient())
        _enable_llm([_search_plan()])
        response = _turn(
            client, account, [{"role": "user", "content": "Staff ML in San Francisco"}]
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["results"] == []
        assert body["steps"][0]["label"] == "Exa not configured"
        assert "not configured" in body["reply"].lower()

    def test_llm_unavailable_joins_user_messages_as_the_query(self, client, account):
        calls = []
        set_exa(ExaClient(api_key="test", transport=_exa_transport(calls)))
        # conftest leaves the LLM disabled; do not set_llm.
        response = _turn(
            client,
            account,
            [
                {"role": "user", "content": "AI infra"},
                {"role": "agent", "content": "Which city?"},
                {"role": "user", "content": "SF staff"},
            ],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["results"]
        assert "Used your message as the search directly." in body["assumptions"]
        search_calls = [c for c in calls if "search" in c["url"]]
        assert search_calls
        assert search_calls[0]["payload"]["query"] == "AI infra SF staff"
