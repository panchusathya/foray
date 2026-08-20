"""The authenticated front door to a paid lookup.

Foray's own route is guarded by a shared secret, but the secret is not an
authorisation decision -- it says "this call came from the API container", not
"a person who is allowed to spend money asked for this". That decision only
exists here, which is why these tests are mostly about who gets turned away.
"""

from __future__ import annotations

import asyncio

import pytest

from juicebox.integrations.foray import ForayClient, ForayError, set_foray

FIND_EMAILS = "/v1/sourcing/find-emails"


class RecordingForay:
    """Duck-typed stand-in for ForayClient. Records, never calls out."""

    def __init__(self, *, configured: bool = True, raises: Exception | None = None) -> None:
        self._configured = configured
        self._raises = raises
        self.calls: list[tuple[list[str], str]] = []

    def is_configured(self) -> bool:
        return self._configured

    async def find_emails(self, logins: list[str], reason: str) -> dict:
        self.calls.append((logins, reason))
        if self._raises:
            raise self._raises
        return {
            "provider": "contactout",
            "summary": {"requested": len(logins), "found_personal": 1},
            "results": {logins[0]: {"status": "found", "personal": ["a@gmail.com"]}},
        }


@pytest.fixture
def foray():
    recorder = RecordingForay()
    set_foray(recorder)
    return recorder


class TestAuthentication:
    def test_no_credentials_is_refused(self, client, foray):
        response = client.post(FIND_EMAILS, json={"logins": ["octocat"], "reason": "shortlist"})
        assert response.status_code == 401
        assert foray.calls == []

    def test_a_bad_token_is_refused(self, client, foray):
        response = client.post(
            FIND_EMAILS,
            json={"logins": ["octocat"], "reason": "shortlist"},
            headers={"Authorization": "Bearer not-a-token"},
        )
        assert response.status_code == 401
        assert foray.calls == []

    def test_an_authenticated_writer_is_allowed(self, client, account, foray):
        response = client.post(
            FIND_EMAILS,
            json={"logins": ["octocat"], "reason": "founding engineer shortlist"},
            headers=account["headers"],
        )
        assert response.status_code == 200, response.text
        assert foray.calls == [(["octocat"], "founding engineer shortlist")]

    def test_a_read_only_principal_cannot_spend(self, client, account, foray, session):
        """Nobody without write permission gets to run up a bill."""
        from juicebox.models import User

        user = session.get(User, account["user_id"])
        user.role = "viewer"
        session.commit()

        response = client.post(
            FIND_EMAILS,
            json={"logins": ["octocat"], "reason": "shortlist"},
            headers=account["headers"],
        )
        assert response.status_code == 403
        assert foray.calls == []


class TestValidation:
    def test_a_reason_is_required(self, client, account, foray):
        """The reason is the only record of why a credit was spent."""
        response = client.post(
            FIND_EMAILS, json={"logins": ["octocat"]}, headers=account["headers"]
        )
        assert response.status_code == 422
        assert foray.calls == []

    def test_a_two_character_reason_is_not_a_reason(self, client, account, foray):
        response = client.post(
            FIND_EMAILS,
            json={"logins": ["octocat"], "reason": "ok"},
            headers=account["headers"],
        )
        assert response.status_code == 422
        assert foray.calls == []

    def test_an_empty_selection_is_refused(self, client, account, foray):
        response = client.post(
            FIND_EMAILS, json={"logins": [], "reason": "shortlist"}, headers=account["headers"]
        )
        assert response.status_code == 422
        assert foray.calls == []

    def test_an_oversized_batch_is_refused_before_it_costs_anything(
        self, client, account, foray
    ):
        """101 logins is a mistake or a script; either way it is refused here
        rather than partially charged for."""
        response = client.post(
            FIND_EMAILS,
            json={"logins": [f"user{n}" for n in range(101)], "reason": "shortlist"},
            headers=account["headers"],
        )
        assert response.status_code == 422
        assert foray.calls == []

    def test_a_full_batch_of_a_hundred_is_allowed(self, client, account, foray):
        response = client.post(
            FIND_EMAILS,
            json={"logins": [f"user{n}" for n in range(100)], "reason": "shortlist"},
            headers=account["headers"],
        )
        assert response.status_code == 200, response.text
        assert len(foray.calls[0][0]) == 100


class TestFailureModes:
    def test_an_unconfigured_deployment_says_so(self, client, account):
        set_foray(RecordingForay(configured=False))
        response = client.post(
            FIND_EMAILS,
            json={"logins": ["octocat"], "reason": "shortlist"},
            headers=account["headers"],
        )
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]

    def test_a_foray_failure_is_a_bad_gateway_not_a_server_error(self, client, account):
        """The caller's request was fine; the service behind us was not. A 500
        here would send someone reading Juicebox's logs for a Foray problem."""
        set_foray(RecordingForay(raises=ForayError("Foray is unreachable: timeout")))
        response = client.post(
            FIND_EMAILS,
            json={"logins": ["octocat"], "reason": "shortlist"},
            headers=account["headers"],
        )
        assert response.status_code == 502
        assert "unreachable" in response.json()["detail"]

    def test_the_provider_detail_reaches_the_caller(self, client, account):
        """"CONTACTOUT_API_KEY is not configured" is actionable; "502" is not."""
        set_foray(RecordingForay(raises=ForayError("CONTACTOUT_API_KEY is not configured")))
        response = client.post(
            FIND_EMAILS,
            json={"logins": ["octocat"], "reason": "shortlist"},
            headers=account["headers"],
        )
        assert "CONTACTOUT_API_KEY" in response.json()["detail"]


class TestClientConfiguration:
    def test_a_client_with_no_token_is_not_configured(self):
        """The default deployment has no token, and must not pretend it can call."""
        assert ForayClient(base_url="http://foray:8100", token="").is_configured() is False

    def test_a_token_is_enough(self):
        assert ForayClient(base_url="http://foray:8100", token="secret").is_configured() is True

    def test_an_empty_base_url_falls_back_to_the_configured_default(self):
        """The token is the half that can genuinely be absent -- FORAY_BASE_URL
        has a working default, so passing "" means "use it", not "unconfigured"."""
        assert ForayClient(base_url="", token="secret").is_configured() is True

    def test_calling_without_configuration_raises_rather_than_requesting(self):
        """No token means no request: a silent no-op would look like "nobody
        had an email" and send someone hunting through Foray's logs."""
        client = ForayClient(base_url="http://foray:8100", token="")
        with pytest.raises(ForayError, match="not configured"):
            asyncio.run(client.find_emails(["octocat"], "shortlist"))

    def test_a_trailing_slash_does_not_double_up_the_path(self):
        client = ForayClient(base_url="http://foray:8100/", token="secret")
        assert client._base_url == "http://foray:8100"
