"""The short portal link, and the legacy JWT it replaced.

A texted magic link used to be ``/m/<jwt>`` -- several hundred characters of
bearer credential closing every message. The grant row was always what made the
link single-use, so the signature was doing no work the row was not.

Three things are worth pinning here:

* the URL is actually short, and unguessable enough for the row to carry the
  security on its own;
* links already sitting in candidates' message histories still work, because
  refusing them would break the one thing a magic link is for;
* the landing path cannot become an open redirect, which the old ``next`` claim
  was never checked for.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox.models import Candidate, CandidateAuthGrant, utcnow
from juicebox.portal.links import (
    mint_portal_link,
    safe_next,
)
from juicebox.repository import upsert_candidates
from juicebox.security import create_access_token


@pytest.fixture
def candidate(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    return session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()


def _code_from(link: str) -> str:
    return link.rsplit("/m/", 1)[-1]


class TestExchangingAShortCode:
    def test_it_opens_a_session_and_lands_on_the_path(
        self, client, session, account, candidate
    ):
        link = mint_portal_link(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            path="/applications/app_123",
            channel="sms",
        )
        session.commit()

        response = client.post(
            "/v1/portal/auth/exchange", json={"token": _code_from(link)}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["next"] == "/applications/app_123"
        assert body["access_token"]
        assert body["candidate"]["first_name"] == "Ada"

    def test_it_is_single_use(self, client, session, account, candidate):
        """The row, not the signature, is what makes this true -- which is the
        whole reason the signature could go."""
        link = mint_portal_link(
            db=session, org_id=account["org_id"], candidate_id=candidate.id
        )
        session.commit()
        assert (
            client.post(
                "/v1/portal/auth/exchange", json={"token": _code_from(link)}
            ).status_code
            == 200
        )
        replay = client.post(
            "/v1/portal/auth/exchange", json={"token": _code_from(link)}
        )
        assert replay.status_code == 401

    def test_an_expired_code_is_refused(self, client, session, account, candidate):
        link = mint_portal_link(
            db=session, org_id=account["org_id"], candidate_id=candidate.id
        )
        grant = session.scalar(select(CandidateAuthGrant))
        grant.expires_at = utcnow() - timedelta(minutes=1)
        session.commit()
        response = client.post(
            "/v1/portal/auth/exchange", json={"token": _code_from(link)}
        )
        assert response.status_code == 401

    def test_an_unknown_code_is_refused(self, client, session, account, candidate):
        response = client.post(
            "/v1/portal/auth/exchange", json={"token": "0" * 20}
        )
        assert response.status_code == 401

    def test_an_empty_code_cannot_match_a_codeless_grant(
        self, client, session, account, candidate
    ):
        """OTP and recruiter grants carry ''. Without the guard, a blank token
        would match the first of them and open somebody's session."""
        grant = CandidateAuthGrant(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            channel="otp",
            expires_at=utcnow() + timedelta(minutes=10),
        )
        session.add(grant)
        session.commit()
        assert grant.link_code == ""
        for token in ("", " ", "\t"):
            assert (
                client.post(
                    "/v1/portal/auth/exchange", json={"token": token}
                ).status_code
                == 401
            )
        session.refresh(grant)
        assert grant.consumed_at is None


class TestCodelessGrantsDoNotCollide:
    def test_two_otp_grants_both_persist(self, session, account, candidate):
        """The partial index. A plain unique index on link_code would make the
        second codeless grant ever issued an IntegrityError, because SQL
        compares '' to '' as equal."""
        for channel in ("otp", "recruiter", "email"):
            session.add(
                CandidateAuthGrant(
                    org_id=account["org_id"],
                    candidate_id=candidate.id,
                    channel=channel,
                    expires_at=utcnow() + timedelta(minutes=10),
                )
            )
        session.commit()
        rows = session.scalars(
            select(CandidateAuthGrant).where(CandidateAuthGrant.link_code == "")
        ).all()
        assert len(rows) == 3


class TestLegacyJwtLinksStillWork:
    """Links already in candidates' phones have not expired yet.

    Deletable once PORTAL_LINK_TTL_SECONDS has elapsed since this shipped, and
    not before -- a magic link that stops working is the one failure a magic
    link cannot have.
    """

    def _legacy_link_token(self, session, account, candidate, *, path="/"):
        from juicebox.config import get_settings

        ttl = get_settings().portal_link_ttl_seconds
        grant = CandidateAuthGrant(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            channel="sms",
            expires_at=utcnow() + timedelta(seconds=ttl),
        )
        session.add(grant)
        session.flush()
        return create_access_token(
            subject=candidate.id,
            org_id=account["org_id"],
            role="candidate",
            ttl_seconds=ttl,
            extra={"typ": "portal-link", "jti": grant.id, "next": path},
        )

    def test_an_old_jwt_link_still_exchanges(
        self, client, session, account, candidate
    ):
        token = self._legacy_link_token(
            session, account, candidate, path="/applications/app_old"
        )
        session.commit()
        response = client.post("/v1/portal/auth/exchange", json={"token": token})
        assert response.status_code == 200, response.text
        assert response.json()["next"] == "/applications/app_old"

    def test_an_old_jwt_link_is_still_single_use(
        self, client, session, account, candidate
    ):
        token = self._legacy_link_token(session, account, candidate)
        session.commit()
        assert (
            client.post("/v1/portal/auth/exchange", json={"token": token}).status_code
            == 200
        )
        assert (
            client.post("/v1/portal/auth/exchange", json={"token": token}).status_code
            == 401
        )

    def test_a_forged_jwt_is_refused(self, client, session, account, candidate):
        assert (
            client.post(
                "/v1/portal/auth/exchange", json={"token": "a.b.c"}
            ).status_code
            == 401
        )

    def test_a_session_token_is_not_a_link(self, client, session, account, candidate):
        """typ discrimination. A week-long session must not be exchangeable for
        another one."""
        from juicebox.portal.links import mint_session_token

        token = mint_session_token(
            org_id=account["org_id"], candidate_id=candidate.id
        )
        assert (
            client.post("/v1/portal/auth/exchange", json={"token": token}).status_code
            == 401
        )


class TestTheLandingPathCannotRedirectAway:
    """`next` is handed to the SPA router. On a link we text to people, an
    absolute URL there is a phishing hop -- and the legacy claim was never
    checked for it at all."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "https://evil.example.com/login",
            "http://evil.example.com",
            "//evil.example.com",
            "javascript:alert(1)",
            "applications/app_1",
        ],
    )
    def test_safe_next_refuses_anything_not_a_path(self, hostile):
        assert safe_next(hostile) == "/"

    def test_minting_stores_a_sanitised_path(self, session, account, candidate):
        mint_portal_link(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            path="https://evil.example.com/steal",
        )
        grant = session.scalar(select(CandidateAuthGrant))
        assert grant.next_path == "/"

    def test_a_legacy_token_with_a_hostile_next_lands_at_root(
        self, client, session, account, candidate
    ):
        """The old claim was unchecked, so this is validated on the way out
        rather than trusted because it was signed."""
        from juicebox.config import get_settings

        ttl = get_settings().portal_link_ttl_seconds
        grant = CandidateAuthGrant(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            channel="sms",
            expires_at=utcnow() + timedelta(seconds=ttl),
        )
        session.add(grant)
        session.flush()
        token = create_access_token(
            subject=candidate.id,
            org_id=account["org_id"],
            role="candidate",
            ttl_seconds=ttl,
            extra={
                "typ": "portal-link",
                "jti": grant.id,
                "next": "https://evil.example.com",
            },
        )
        session.commit()
        response = client.post("/v1/portal/auth/exchange", json={"token": token})
        assert response.status_code == 200
        assert response.json()["next"] == "/"
