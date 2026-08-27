"""Portal auth: single-use links, the OTP path, and the wall between tokens.

The critical pair lives in ``TestTokenWall``: a candidate session on a
recruiter route is a 401, and a recruiter token on a portal route is a 401.
Everything signs with one SECRET_KEY, so the ``typ`` claim checks in deps.py
are the only wall — these tests are what keep them from being refactored away.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox.models import Candidate, CandidateAuthGrant, PortalOtp, utcnow
from juicebox.portal.links import mint_portal_link, mint_session_token
from juicebox.repository import upsert_candidates


@pytest.fixture
def candidate(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    return session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()


def _token_from(link: str) -> str:
    return link.rsplit("/m/", 1)[-1]


def _session_headers(session, account, candidate) -> dict:
    token = mint_session_token(org_id=account["org_id"], candidate_id=candidate.id)
    return {"Authorization": f"Bearer {token}"}


class TestMagicLink:
    def test_mint_exchange_and_single_use(self, client, session, account, candidate):
        link = mint_portal_link(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            path="/applications/app_123",
            channel="sms",
        )
        session.commit()
        assert "/m/" in link

        response = client.post("/v1/portal/auth/exchange", json={"token": _token_from(link)})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["next"] == "/applications/app_123"
        assert body["candidate"]["first_name"] == "Ada"
        assert body["access_token"]

        # The grant is spent; a replay (forwarded screenshot, history) is 401.
        replay = client.post("/v1/portal/auth/exchange", json={"token": _token_from(link)})
        assert replay.status_code == 401

    def test_expired_grant_is_refused(self, client, session, account, candidate):
        link = mint_portal_link(
            db=session, org_id=account["org_id"], candidate_id=candidate.id
        )
        grant = session.scalar(select(CandidateAuthGrant))
        grant.expires_at = utcnow() - timedelta(minutes=1)
        session.commit()
        response = client.post("/v1/portal/auth/exchange", json={"token": _token_from(link)})
        assert response.status_code == 401

    def test_a_link_token_is_not_a_session(self, client, session, account, candidate):
        """typ=portal-link opens nothing directly — it must be exchanged."""
        link = mint_portal_link(
            db=session, org_id=account["org_id"], candidate_id=candidate.id
        )
        session.commit()
        response = client.get(
            "/v1/portal/me", headers={"Authorization": f"Bearer {_token_from(link)}"}
        )
        assert response.status_code == 401

    def test_recruiter_endpoint_mints_a_working_link(
        self, client, session, account, candidate
    ):
        session.commit()
        minted = client.post(
            f"/v1/candidates/{candidate.id}/portal-link", headers=account["headers"]
        )
        assert minted.status_code == 200, minted.text
        token = _token_from(minted.json()["url"])
        exchanged = client.post("/v1/portal/auth/exchange", json={"token": token})
        assert exchanged.status_code == 200


class TestTokenWall:
    def test_candidate_session_cannot_open_recruiter_routes(
        self, client, session, account, candidate
    ):
        session.commit()
        headers = _session_headers(session, account, candidate)
        for path in ("/v1/candidates", "/v1/applications", "/v1/sequences"):
            response = client.get(path, headers=headers)
            assert response.status_code == 401, path

    def test_recruiter_token_cannot_open_portal_routes(self, client, session, account):
        session.commit()
        for path in ("/v1/portal/me", "/v1/portal/feed", "/v1/portal/applications"):
            response = client.get(path, headers=account["headers"])
            assert response.status_code == 401, path

    def test_portal_route_works_with_the_right_token(
        self, client, session, account, candidate
    ):
        session.commit()
        response = client.get(
            "/v1/portal/me", headers=_session_headers(session, account, candidate)
        )
        assert response.status_code == 200


class TestOtp:
    def _request(self, client, email):
        return client.post("/v1/portal/auth/otp/request", json={"email": email})

    def _sent_code(self):
        from juicebox.outreach.sender import get_sender

        sender = get_sender()
        assert sender.outbox, "no OTP email was sent"
        subject = sender.outbox[-1].subject
        return subject.rsplit(":", 1)[-1].strip()

    def test_unknown_email_is_still_202(self, client, session):
        session.commit()
        response = self._request(client, "nobody@example.com")
        assert response.status_code == 202

    def test_request_verify_round_trip(self, client, session, account, candidate):
        session.commit()
        assert self._request(client, "ada@example.com").status_code == 202
        code = self._sent_code()

        verified = client.post(
            "/v1/portal/auth/otp/verify", json={"email": "ada@example.com", "code": code}
        )
        assert verified.status_code == 200, verified.text
        token = verified.json()["access_token"]
        me = client.get("/v1/portal/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200

        # Consumed: the same code opens nothing twice.
        again = client.post(
            "/v1/portal/auth/otp/verify", json={"email": "ada@example.com", "code": code}
        )
        assert again.status_code == 401

    def test_wrong_codes_hard_fail_at_the_cap(self, client, session, account, candidate):
        session.commit()
        self._request(client, "ada@example.com")
        code = self._sent_code()
        for _ in range(5):
            response = client.post(
                "/v1/portal/auth/otp/verify",
                json={"email": "ada@example.com", "code": "000000"},
            )
            assert response.status_code == 401
        # Even the RIGHT code is dead now: five wrong guesses burn the OTP.
        response = client.post(
            "/v1/portal/auth/otp/verify", json={"email": "ada@example.com", "code": code}
        )
        assert response.status_code == 401

    def test_hourly_issuance_cap(self, client, session, account, candidate):
        session.commit()
        for _ in range(3):
            assert self._request(client, "ada@example.com").status_code == 202
        rows = session.scalars(select(PortalOtp)).all()
        assert len(rows) == 3
        # The fourth is a silent 202: same answer, no new code, no email.
        assert self._request(client, "ada@example.com").status_code == 202
        session.expire_all()
        assert len(session.scalars(select(PortalOtp)).all()) == 3


class TestVaultStepUp:
    def _session_headers(self, account, candidate):
        return {
            "Authorization": (
                f"Bearer {mint_session_token(org_id=account['org_id'], candidate_id=candidate.id)}"
            )
        }

    def _sent_code(self):
        from juicebox.outreach.sender import get_sender

        return get_sender().outbox[-1].subject.rsplit(":", 1)[-1].strip()

    def test_the_portal_session_authorizes_a_vault_write(self, client, session, account, candidate):
        """The step-up OTP was removed (operator decision 2026-08-27): it rode
        the same unreliable mail path that made the vault unreachable, and a
        candidate arriving on a single-use texted link has already
        authenticated. The write is accepted on the session alone, the
        password never comes back, and a blank username is derived."""
        session.commit()
        headers = self._session_headers(account, candidate)
        payload = {
            "vendor": "workday",
            "tenant": "acme",
            "password": "Horse-Battery9!",
        }
        created = client.post("/v1/portal/vault/accounts", headers=headers, json=payload)
        assert created.status_code == 201, created.text
        body = created.json()
        assert "password" not in body
        assert body["username"]  # derived server-side
        assert body["status"] == "pending_verification"

    def test_a_weak_password_is_refused_with_the_missing_rules(self, client, session, account, candidate):
        """The employer's complexity rule is enforced at the door so a
        registration never fails on it later."""
        session.commit()
        headers = self._session_headers(account, candidate)
        weak = client.post(
            "/v1/portal/vault/accounts",
            headers=headers,
            json={"vendor": "workday", "tenant": "acme", "password": "alllowercase"},
        )
        assert weak.status_code == 422
        detail = weak.json()["detail"].lower()
        assert "uppercase" in detail and "number" in detail and "special" in detail


class TestPortalSender:
    """Who a sign-in code appears to come from.

    Outreach is a named recruiter writing to someone they are courting. A
    sign-in code is the system talking, and a candidate who has never met
    that recruiter should not get an unexplained email from their personal
    mailbox -- nor should replies land where nobody reads them.
    """

    def _reload(self, monkeypatch, **env):
        import juicebox.config as config

        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        config.reset_settings_cache()

    def test_falls_back_to_the_outreach_identity_when_unset(self, monkeypatch):
        from juicebox.portal.mail import portal_sender

        self._reload(
            monkeypatch,
            PORTAL_FROM_EMAIL="",
            OUTREACH_FROM_EMAIL="recruiter@example.com",
            OUTREACH_FROM_NAME="Dana",
        )
        assert portal_sender() == ("recruiter@example.com", "Dana")

    def test_a_configured_address_brings_its_own_name(self, monkeypatch):
        """The pairing rule: address and display name move together.

        Taking the portal address but keeping the outreach display name would
        put one person's name on another person's mailbox, which reads as
        impersonation to anyone who looks at the header.
        """
        from juicebox.portal.mail import portal_sender

        self._reload(
            monkeypatch,
            PORTAL_FROM_EMAIL="vivian@example.com",
            PORTAL_FROM_NAME="Foray",
            OUTREACH_FROM_EMAIL="recruiter@example.com",
            OUTREACH_FROM_NAME="Dana",
        )
        email, name = portal_sender()
        assert (email, name) == ("vivian@example.com", "Foray")
        assert name != "Dana", "the outreach recruiter's name must not ride along"

    def test_the_otp_email_actually_carries_it(
        self, client, session, account, candidate, monkeypatch, mailbox
    ):
        self._reload(
            monkeypatch,
            PORTAL_FROM_EMAIL="vivian@example.com",
            PORTAL_FROM_NAME="Foray",
            OUTREACH_FROM_EMAIL="recruiter@example.com",
            OUTREACH_FROM_NAME="Dana",
        )
        session.commit()
        assert (
            client.post(
                "/v1/portal/auth/otp/request", json={"email": "ada@example.com"}
            ).status_code
            == 202
        )
        sent = mailbox.outbox[-1]
        assert sent.from_email == "vivian@example.com"
        assert sent.from_name == "Foray"


class TestGoogleSignIn:
    """The door that works when email does not.

    Outbound SMTP is blocked at the network level on the production host, so
    the OTP path can accept an address and still never deliver anything.
    Google sends no email at all.
    """

    @pytest.fixture(autouse=True)
    def google_configured(self, monkeypatch):
        import juicebox.config as config

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
        config.reset_settings_cache()
        yield
        config.reset_settings_cache()

    def _tokeninfo(self, monkeypatch, payload, code=200):
        """Stand in for Google's tokeninfo endpoint."""
        import juicebox.portal.google as google

        class Response:
            # Bound from the argument, not named the same: a class body cannot
            # read the enclosing function's locals.
            status_code = code

            def json(self):
                return payload

        monkeypatch.setattr(google.httpx, "get", lambda *a, **k: Response())

    def _claims(self, **over):
        base = {
            "aud": "test-client.apps.googleusercontent.com",
            "iss": "https://accounts.google.com",
            "email": "ada@example.com",
            "email_verified": "true",
            "name": "Ada Okonkwo",
            "sub": "12345",
        }
        base.update(over)
        return base

    def test_a_verified_google_account_gets_a_session(
        self, client, session, account, candidate, monkeypatch
    ):
        self._tokeninfo(monkeypatch, self._claims())
        session.commit()

        response = client.post("/v1/portal/auth/google", json={"credential": "x"})
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        me = client.get("/v1/portal/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200

    def test_a_token_for_another_app_is_refused(
        self, client, session, account, candidate, monkeypatch
    ):
        """The audience check is the whole point.

        A Google token issued for somebody else's client is still a perfectly
        valid Google token. Without checking `aud`, anyone with any Google
        application could mint sessions here for any address they control.
        """
        self._tokeninfo(monkeypatch, self._claims(aud="someone-elses-app"))
        session.commit()
        response = client.post("/v1/portal/auth/google", json={"credential": "x"})
        assert response.status_code == 401

    def test_an_unverified_address_is_refused(
        self, client, session, account, candidate, monkeypatch
    ):
        """Unverified proves nothing about who holds the address."""
        self._tokeninfo(monkeypatch, self._claims(email_verified="false"))
        session.commit()
        response = client.post("/v1/portal/auth/google", json={"credential": "x"})
        assert response.status_code == 401

    def test_an_unexpected_issuer_is_refused(
        self, client, session, account, candidate, monkeypatch
    ):
        self._tokeninfo(monkeypatch, self._claims(iss="https://evil.example.com"))
        session.commit()
        response = client.post("/v1/portal/auth/google", json={"credential": "x"})
        assert response.status_code == 401

    def test_an_unknown_address_says_so(self, client, session, account, monkeypatch):
        """Unlike OTP, this one tells the truth.

        The OTP door answers 202 to everything so it cannot be used to find out
        who is a candidate. Here the caller has already proved they own the
        address, so telling them we have no record tells them only about
        themselves -- and silence would leave them clicking a dead button.
        """
        self._tokeninfo(monkeypatch, self._claims(email="stranger@example.com"))
        session.commit()
        response = client.post("/v1/portal/auth/google", json={"credential": "x"})
        assert response.status_code == 404

    def test_the_config_endpoint_reports_the_client_id(self, client):
        response = client.get("/v1/portal/auth/config")
        assert response.status_code == 200
        assert response.json()["google_client_id"].endswith(".apps.googleusercontent.com")

    def test_unconfigured_deployments_refuse_and_advertise_nothing(
        self, client, monkeypatch
    ):
        import juicebox.config as config

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
        config.reset_settings_cache()
        assert client.get("/v1/portal/auth/config").json()["google_client_id"] == ""
        assert client.post("/v1/portal/auth/google", json={"credential": "x"}).status_code == 401
