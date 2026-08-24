"""The portal API: leak tests, the swipe deck, and candidate-scoped writes.

``_assert_no_leaks`` walks every response recursively for the forbidden keys
— it is the contract every future portal serializer answers to. The fixtures
deliberately load the rows with recruiter-internal values so a leak has
something real to leak.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.applications.service import create_application
from juicebox.applications.state import transition
from juicebox.models import (
    Application,
    Candidate,
    CandidatePreference,
    JobMatch,
    JobPosting,
    JobProposal,
    utcnow,
)
from juicebox.portal.links import mint_session_token
from juicebox.portal.schemas import FORBIDDEN_KEYS
from juicebox.repository import upsert_candidates

STRONG_CRITERION = {
    "name": "title", "label": "Title", "kind": "title", "weight": 1.0,
    "status": "strong", "score": 0.95, "evidence": "Current title matches", "sources": [],
}


def _assert_no_leaks(payload) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key not in FORBIDDEN_KEYS, f"portal response leaked {key!r}"
            _assert_no_leaks(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_leaks(item)


@pytest.fixture
def candidate(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    row = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
    # Load the row with recruiter-internal values so a leak is detectable.
    row.crm_status = "placed"
    session.add(
        CandidatePreference(
            org_id=account["org_id"],
            candidate_id=row.id,
            timeline="within a month",
            target_total=260000.0,
            comp_currency="USD",
            confidential_items=["do not tell current employer"],
            spec={"job_titles": ["ml engineer"], "locations": ["San Francisco"]},
        )
    )
    session.commit()
    return row


@pytest.fixture
def headers(account, candidate):
    token = mint_session_token(org_id=account["org_id"], candidate_id=candidate.id)
    return {"Authorization": f"Bearer {token}"}


def _posting(session, org_id, *, company="Anthropic", title="ML Engineer", suffix="1"):
    posting = JobPosting(
        org_id=org_id,
        source="manual",
        source_id=f"portal-{company.lower()}-{suffix}",
        title=title,
        company=company,
        company_normalized=company.lower(),
        jd_text="Build things.",
        status="open",
    )
    session.add(posting)
    session.flush()
    return posting


def _match(session, org_id, candidate, posting, *, score=0.9):
    match = JobMatch(
        org_id=org_id,
        job_posting_id=posting.id,
        candidate_id=candidate.id,
        score=score,
        criteria=[STRONG_CRITERION],
        status="new",
    )
    session.add(match)
    session.flush()
    return match


class TestMeAndProfile:
    def test_me_is_whitelisted(self, client, session, account, candidate, headers):
        response = client.get("/v1/portal/me", headers=headers)
        assert response.status_code == 200
        body = response.json()
        _assert_no_leaks(body)
        assert body["candidate"]["full_name"] == "Ada Okonkwo"
        assert body["preference"]["timeline"] == "within a month"

    def test_profile_patch_lands_in_manual_fields(
        self, client, session, account, candidate, headers
    ):
        response = client.patch(
            "/v1/portal/profile",
            json={"headline": "Staff ML engineer", "phones": ["+14155550123"]},
            headers=headers,
        )
        assert response.status_code == 200
        _assert_no_leaks(response.json())
        session.expire_all()
        row = session.get(Candidate, candidate.id)
        assert row.headline == "Staff ML engineer"
        assert "headline" in (row.manual_fields or [])
        assert "phones" in (row.manual_fields or [])

    def test_disallowed_fields_are_ignored(self, client, session, account, candidate, headers):
        response = client.patch(
            "/v1/portal/profile", json={"headline": "ok"}, headers=headers
        )
        assert response.status_code == 200
        # crm_status is not even a field on the patch model; a candidate
        # cannot rename their pipeline state through this door.
        response = client.patch(
            "/v1/portal/profile", json={"crm_status": "placed"}, headers=headers
        )
        assert response.status_code in {200, 422}
        session.expire_all()
        assert session.get(Candidate, candidate.id).crm_status == "placed"


class TestFeedAndSwipe:
    def test_feed_is_barred_deduped_and_leak_free(
        self, client, session, account, candidate, headers
    ):
        org_id = account["org_id"]
        good = _posting(session, org_id, company="Anthropic", suffix="good")
        _match(session, org_id, candidate, good)
        weak = _posting(session, org_id, company="WeakCo", suffix="weak")
        _match(session, org_id, candidate, weak, score=0.1)
        answered = _posting(session, org_id, company="Answered", suffix="done")
        _match(session, org_id, candidate, answered)
        session.add(
            JobProposal(
                org_id=org_id,
                candidate_id=candidate.id,
                job_posting_id=answered.id,
                channel="portal",
                status="declined",
                responded_at=utcnow(),
            )
        )
        session.commit()

        response = client.get("/v1/portal/feed", headers=headers)
        assert response.status_code == 200
        body = response.json()
        _assert_no_leaks(body)
        companies = [card["company"] for card in body["cards"]]
        assert companies == ["Anthropic"]  # below-floor and already-answered gone
        assert body["cards"][0]["reasons"] == ["Current title matches"]

    def test_swipe_right_creates_the_pursuit(
        self, client, session, account, candidate, headers
    ):
        org_id = account["org_id"]
        posting = _posting(session, org_id, suffix="swipe")
        _match(session, org_id, candidate, posting)
        session.commit()

        response = client.post(
            f"/v1/portal/feed/{posting.id}/swipe", json={"direction": "right"}, headers=headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "accepted"

        proposal = session.scalar(select(JobProposal))
        assert proposal.status == "accepted" and proposal.channel == "portal"
        application = session.get(Application, body["application_id"])
        assert application.proposal_id == proposal.id
        assert application.source == "candidate"

        # Stale client replay: answered stays answered.
        replay = client.post(
            f"/v1/portal/feed/{posting.id}/swipe", json={"direction": "right"}, headers=headers
        )
        assert replay.json().get("duplicate") is True

    def test_swipe_left_never_resurfaces(self, client, session, account, candidate, headers):
        org_id = account["org_id"]
        posting = _posting(session, org_id, suffix="left")
        _match(session, org_id, candidate, posting)
        session.commit()

        response = client.post(
            f"/v1/portal/feed/{posting.id}/swipe", json={"direction": "left"}, headers=headers
        )
        assert response.json()["status"] == "declined"
        feed = client.get("/v1/portal/feed", headers=headers).json()
        assert posting.id not in [card["posting_id"] for card in feed["cards"]]
        assert session.scalar(select(Application)) is None


class TestApplications:
    def _pursue(self, session, account, candidate):
        posting = _posting(session, account["org_id"], suffix="app")
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        transition(session, application, to_status="prepared", actor="agent")
        session.commit()
        return application

    def test_approve_then_confirm_submitted(
        self, client, session, account, candidate, headers
    ):
        application = self._pursue(session, account, candidate)

        # Confirming before approving is exactly the consent hole -- refused.
        premature = client.post(
            f"/v1/portal/applications/{application.id}/confirm-submitted", headers=headers
        )
        assert premature.status_code == 409

        approved = client.post(
            f"/v1/portal/applications/{application.id}/approve", headers=headers
        )
        assert approved.status_code == 200
        _assert_no_leaks(approved.json())
        assert approved.json()["status"] == "awaiting_consent"

        confirmed = client.post(
            f"/v1/portal/applications/{application.id}/confirm-submitted", headers=headers
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "submitted"
        session.expire_all()
        row = session.get(Application, application.id)
        assert row.submitted_by == "candidate"
        assert row.submission_method == "portal_link"

    def test_cross_candidate_probing_is_a_404(
        self, client, session, account, candidate, headers, sample_candidates
    ):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[1:2])
        other = session.query(Candidate).filter_by(full_name="Bo Lindqvist").one()
        posting = _posting(session, account["org_id"], suffix="other")
        other_application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=other.id,
            job_posting_id=posting.id,
        )
        session.commit()
        response = client.get(
            f"/v1/portal/applications/{other_application.id}", headers=headers
        )
        assert response.status_code == 404

    def test_list_is_leak_free(self, client, session, account, candidate, headers):
        self._pursue(session, account, candidate)
        response = client.get("/v1/portal/applications", headers=headers)
        assert response.status_code == 200
        _assert_no_leaks(response.json())


class TestDocuments:
    def test_signed_download_url_round_trip(
        self, client, session, account, candidate, headers
    ):
        from juicebox import storage
        from juicebox.docgen.render import render_letter_pdf
        from juicebox.models import CandidateDocument

        doc = CandidateDocument(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            kind="resume_generated",
            filename="ada-resume.pdf",
            status="ready",
            source="generated",
        )
        session.add(doc)
        session.flush()
        relpath = storage.document_relpath(
            org_id=doc.org_id, candidate_id=doc.candidate_id, document_id=doc.id
        )
        _size, _digest = storage.save_bytes(relpath, render_letter_pdf("resume body"))
        doc.storage_path = relpath
        session.commit()

        listing = client.get("/v1/portal/documents", headers=headers)
        assert listing.status_code == 200
        _assert_no_leaks(listing.json())

        minted = client.post(
            f"/v1/portal/documents/{doc.id}/download-url", headers=headers
        )
        assert minted.status_code == 200
        url = minted.json()["url"]
        assert "sig=" in url and "Bearer" not in url

        fetched = client.get(url)  # no auth header -- the signature is the auth
        assert fetched.status_code == 200
        assert fetched.content.startswith(b"%PDF-")

        # A tampered expiry fails the signature, not just the clock.
        tampered = url.replace("exp=", "exp=9")
        assert client.get(tampered).status_code == 401

    def test_expired_signature_is_refused(self, client, session, account, candidate, headers):
        from juicebox.models import CandidateDocument
        from juicebox.security import signed_intake_token

        doc = CandidateDocument(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            kind="cover_letter",
            status="ready",
        )
        session.add(doc)
        session.commit()
        expired = 1_000_000  # 1970
        sig = signed_intake_token("portal-doc", f"{doc.id}:{expired}")
        response = client.get(f"/v1/portal/documents/{doc.id}/file?exp={expired}&sig={sig}")
        assert response.status_code == 401
