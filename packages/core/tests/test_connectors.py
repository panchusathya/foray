"""Provider connectors against stubbed wire responses — no network, no creds.

Each test monkeypatches the module's ``httpx.Client`` with a MockTransport,
the ``test_zoho`` pattern, and asserts two things per provider: the OAuth
exchange normalises correctly, and the reads stay targeted (fixed queries,
metadata listings, bytes only for the one chosen attachment).
"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from juicebox.config import reset_settings_cache
from juicebox.connectors import github as github_mod
from juicebox.connectors import google as google_mod
from juicebox.connectors import microsoft as microsoft_mod
from juicebox.connectors.base import ConnectorError
from juicebox.connectors.github import GithubConnector
from juicebox.connectors.google import GoogleConnector
from juicebox.connectors.microsoft import MicrosoftCandidateConnector
from juicebox.integrations.crypto import encrypt_json


class _DummyDB:
    def flush(self) -> None:
        return None


def _connection() -> SimpleNamespace:
    """A connection-shaped row holding an unexpired token, so the shared
    token helper never tries to refresh mid-test."""
    return SimpleNamespace(
        id="ccon_test",
        token_blob=encrypt_json(
            {"access_token": "tok", "refresh_token": "ref", "expires_at": time.time() + 3600}
        ),
        status="active",
        error="",
        display_name="Ada Lovelace",
    )


def _stub(monkeypatch, module, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    real_client = httpx.Client  # module.httpx IS this httpx; capture before patching

    monkeypatch.setattr(
        module.httpx, "Client", lambda **kwargs: real_client(transport=transport)
    )
    return seen


@pytest.fixture
def google_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CONNECT_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CONNECT_CLIENT_SECRET", "gsecret")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def microsoft_env(monkeypatch):
    monkeypatch.setenv("MS_CANDIDATE_CLIENT_ID", "mid")
    monkeypatch.setenv("MS_CANDIDATE_CLIENT_SECRET", "msecret")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def github_env(monkeypatch):
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "ghid")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "ghsecret")
    reset_settings_cache()
    yield
    reset_settings_cache()


class TestGoogle:
    def test_exchange_code_normalises_tokens_and_identity(self, monkeypatch, google_env):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(
                    200,
                    json={"access_token": "at", "refresh_token": "rt", "expires_in": 3599},
                )
            return httpx.Response(
                200, json={"email": "Ada@Gmail.com", "name": "Ada", "sub": "sub-1"}
            )

        _stub(monkeypatch, google_mod, handler)
        grant = GoogleConnector().exchange_code("code", {})
        assert grant.address == "ada@gmail.com"
        assert grant.tokens["access_token"] == "at"
        assert grant.tokens["refresh_token"] == "rt"
        assert grant.tokens["expires_at"] > time.time()

    def test_list_resume_hits_runs_only_the_fixed_queries(self, monkeypatch, google_env):
        message = {
            "id": "m1",
            "internalDate": "1700000000000",
            "payload": {
                "parts": [
                    {"filename": "", "body": {"size": 12}},
                    {
                        "filename": "ada-resume.pdf",
                        "mimeType": "application/pdf",
                        "body": {"attachmentId": "a1", "size": 4096},
                    },
                ]
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/messages"):
                return httpx.Response(200, json={"messages": [{"id": "m1"}]})
            return httpx.Response(200, json=message)

        seen = _stub(monkeypatch, google_mod, handler)
        hits = GoogleConnector().list_resume_hits(_DummyDB(), _connection())

        queries = [
            request.url.params.get("q")
            for request in seen
            if request.url.path.endswith("/messages")
        ]
        assert queries == [query for query, _ in google_mod.RESUME_QUERIES]
        # Same message from both queries: listed once, credited to sent.
        assert len(hits) == 1
        assert hits[0].native_id == "m1:a1"
        assert hits[0].sent_by_owner is True
        assert hits[0].filename == "ada-resume.pdf"

    def test_fetch_resume_decodes_urlsafe_base64(self, monkeypatch, google_env):
        payload = base64.urlsafe_b64encode(b"%PDF-1.4 fake").decode().rstrip("=")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/messages/m1/attachments/a1")
            return httpx.Response(200, json={"data": payload, "size": 13})

        _stub(monkeypatch, google_mod, handler)
        data = GoogleConnector().fetch_resume(
            _DummyDB(), _connection(), SimpleNamespace(native_id="m1:a1")
        )
        assert data == b"%PDF-1.4 fake"


class TestMicrosoft:
    def test_exchange_code_reads_me_for_the_address(self, monkeypatch, microsoft_env):
        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2" in request.url.path:
                body = dict(pair.split("=", 1) for pair in request.read().decode().split("&"))
                assert body["scope"] == "offline_access+User.Read+Mail.Read"
                return httpx.Response(
                    200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
                )
            return httpx.Response(
                200,
                json={"mail": "Ada@Outlook.com", "displayName": "Ada", "id": "ms-1"},
            )

        _stub(monkeypatch, microsoft_mod, handler)
        grant = MicrosoftCandidateConnector().exchange_code("code", {})
        assert grant.address == "ada@outlook.com"
        assert grant.scopes == "offline_access User.Read Mail.Read"

    def test_tenant_defaults_to_common_for_personal_accounts(self, microsoft_env):
        url = MicrosoftCandidateConnector().authorize_url("state-1")
        assert "/common/oauth2/v2.0/authorize" in url
        assert "Mail.Read" in url
        assert "Mail.Send" not in url

    def test_list_resume_hits_walks_both_folders_metadata_only(
        self, monkeypatch, microsoft_env
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "mailFolders" in path:
                folder = "s1" if "sentitems" in path else "i1"
                return httpx.Response(
                    200,
                    json={"value": [{"id": folder, "sentDateTime": "2025-01-02T03:04:05Z"}]},
                )
            assert path.endswith("/attachments")
            assert request.url.params["$select"] == "id,name,contentType,size,isInline"
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "a1", "name": "resume.pdf", "contentType": "application/pdf",
                         "size": 100, "isInline": False},
                        {"id": "a2", "name": "logo.png", "contentType": "image/png",
                         "size": 10, "isInline": True},
                    ]
                },
            )

        _stub(monkeypatch, microsoft_mod, handler)
        hits = MicrosoftCandidateConnector().list_resume_hits(_DummyDB(), _connection())
        assert [h.native_id for h in hits] == ["s1|a1", "i1|a1"]
        assert hits[0].sent_by_owner and not hits[1].sent_by_owner


class TestGithub:
    def test_exchange_code_refuses_a_bad_code(self, monkeypatch, github_env):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": "bad_verification_code"})

        _stub(monkeypatch, github_mod, handler)
        with pytest.raises(ConnectorError):
            GithubConnector().exchange_code("nope", {})

    def test_profile_carries_haves_not_a_guessed_title(self, monkeypatch, github_env):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/user":
                return httpx.Response(
                    200,
                    json={
                        "login": "ada",
                        "name": "Ada Lovelace",
                        "company": "@Stripe",
                        "location": "London",
                        "bio": "I write compilers",
                        "html_url": "https://github.com/ada",
                    },
                )
            assert request.url.path == "/users/ada/repos"
            return httpx.Response(
                200, json=[{"language": "Rust", "stargazers_count": 3, "fork": False}]
            )

        _stub(monkeypatch, github_mod, handler)
        profile = GithubConnector().fetch_profile(_DummyDB(), _connection())
        assert profile.full_name == "Ada Lovelace"
        assert profile.current_employer == "Stripe"
        assert profile.skills == ("Rust",)
        # A bio is not a job title; the flow's gap-fill asks instead.
        assert profile.current_title == ""


class TestGoogleSignal:
    def test_signal_is_a_count_never_content(self, monkeypatch, google_env):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["maxResults"] == "1"
            return httpx.Response(200, json={"resultSizeEstimate": 4})

        _stub(monkeypatch, google_mod, handler)
        signal = google_mod.job_search_signal(GoogleConnector(), _DummyDB(), _connection())
        assert signal == {"applications_sent_last_year": 4}


def _load_json(request: httpx.Request) -> dict:
    return json.loads(request.read().decode() or "{}")
