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


class TestClientConfiguration:
    def test_a_client_with_no_token_is_not_configured(self):
        """The default deployment has no token, and must not pretend it can call."""
        assert ForayClient(base_url="http://foray:8100", token="").is_configured() is False


    def test_calling_without_configuration_raises_rather_than_requesting(self):
        """No token means no request: a silent no-op would look like "nobody
        had an email" and send someone hunting through Foray's logs."""
        client = ForayClient(base_url="http://foray:8100", token="")
        with pytest.raises(ForayError, match="not configured"):
            asyncio.run(client.find_emails(["octocat"], "shortlist"))

