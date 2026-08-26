"""A fully scripted connector, for tests and offline demos.

Never registered in production by default: tests install it with
``set_connector("fake", ...)`` and a dev box opts in with
``JUICEBOX_FAKE_CONNECTOR=1``. Everything it returns is constructor-supplied,
so a test can stage a resume, a profile-only grant, or any failure without a
network or an OAuth app — the same job ``FakeChannel`` does for delivery.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .base import ConnectorError, ConnectorGrant, ConnectorProfile, ResumeHit


class FakeConnector:
    name = "fake"
    label = "TestMail"

    def __init__(
        self,
        *,
        name: str = "fake",
        label: str = "TestMail",
        grant: ConnectorGrant | None = None,
        hits: list[ResumeHit] | None = None,
        resume_bytes: bytes = b"",
        profile: ConnectorProfile | None = None,
        configured: bool = True,
        fail_index: str = "",
    ) -> None:
        self.name = name
        self.label = label
        self.grant = grant or ConnectorGrant(
            tokens={"access_token": "fake-token", "refresh_token": "", "expires_at": 2**33},
            address="fake@example.com",
            display_name="Fake Person",
            provider_account_id="fake-1",
            scopes="fake.read",
        )
        self.hits = list(hits or [])
        self.resume_bytes = resume_bytes
        self.profile = profile or ConnectorProfile()
        self.configured = configured
        # "" | "hits" | "resume" | "profile" — which call should blow up.
        self.fail_index = fail_index
        self.exchanged: list[str] = []
        self.revoked: list[str] = []

    def is_configured(self) -> bool:
        return self.configured

    def redirect_uri(self) -> str:
        return f"http://localhost:8000/v1/connect/{self.name}/callback"

    def authorize_url(self, state: str) -> str:
        return f"https://connect.example/{self.name}/authorize?state={state}"

    def exchange_code(self, code: str, params: Mapping[str, str]) -> ConnectorGrant:
        self.exchanged.append(code)
        if code == "bad":
            raise ConnectorError("the code was refused")
        return self.grant

    def list_resume_hits(self, db, connection) -> list[ResumeHit]:
        if self.fail_index == "hits":
            raise ConnectorError("listing failed")
        return list(self.hits)

    def fetch_resume(self, db, connection, hit: ResumeHit) -> bytes:
        if self.fail_index == "resume":
            raise ConnectorError("download failed")
        return self.resume_bytes

    def fetch_profile(self, db, connection) -> ConnectorProfile:
        if self.fail_index == "profile":
            raise ConnectorError("profile fetch failed")
        return self.profile

    def revoke(self, db, connection) -> None:
        self.revoked.append(connection.id)


def hit(
    *,
    native_id: str = "m1:a1",
    filename: str = "resume.pdf",
    sent_by_owner: bool = True,
    occurred_at: datetime | None = None,
    size_bytes: int = 1024,
) -> ResumeHit:
    """A ResumeHit with sensible test defaults."""
    return ResumeHit(
        native_id=native_id,
        filename=filename,
        content_type="application/pdf",
        sent_by_owner=sent_by_owner,
        occurred_at=occurred_at,
        size_bytes=size_bytes,
    )
