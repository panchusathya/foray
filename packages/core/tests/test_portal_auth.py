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
