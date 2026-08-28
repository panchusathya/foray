"""Contract coverage for the separate OpenInstinct candidate deployment."""

from __future__ import annotations

from unittest.mock import patch

from juicebox.applications.state import transition
from juicebox.config import reset_settings_cache
from juicebox.models import (
    Application,
    ApplicationSubmission,
    Candidate,
    JobMatch,
    JobPosting,
    OpenInstinctApplicationTask,
)
from juicebox.openinstinct import JUICEBOX_AUDIENCE, mint_bridge_token
from juicebox.repository import upsert_candidates


def _service_headers(*, subject: str = "better-auth:openinstinct-user", identities=None):
    token = mint_bridge_token(
        audience=JUICEBOX_AUDIENCE,
        subject=subject,
        identities=(
            [{"kind": "email", "value": "ada@example.com", "verified": True}]
            if identities is None
            else identities
        ),
    )
    return {"Authorization": f"Bearer {token}"}


def _enable_bridge(monkeypatch) -> None:
    monkeypatch.setenv("OPENINSTINCT_SHARED_SECRET", "s" * 32)
    reset_settings_cache()


def _candidate(session, account, sample_candidates) -> Candidate:
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    return session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()


def _posting(session, org_id: str) -> JobPosting:
    posting = JobPosting(
        org_id=org_id,
        source="manual",
        source_id="openinstinct-application",
        title="Machine Learning Engineer",
        company="Example AI",
        company_normalized="example ai",
        source_url="https://jobs.example.test/ml",
        apply_url="https://jobs.example.test/ml/apply",
        status="open",
    )
    session.add(posting)
    session.commit()
    return posting


def _matched_posting(session, candidate: Candidate) -> JobPosting:
    posting = _posting(session, candidate.org_id)
    session.add(
        JobMatch(
            org_id=candidate.org_id,
            candidate_id=candidate.id,
            job_posting_id=posting.id,
            score=0.95,
            criteria=[
                {
                    "label": "Relevant experience",
                    "score": 0.95,
                    "evidence": "Relevant machine learning experience",
                }
            ],
        )
    )
    session.commit()
    return posting


def _link(client):
    response = client.post(
        "/v1/internal/openinstinct/identity-links",
        json={
            "external_user_id": "better-auth:openinstinct-user",
            "email": "ada@example.com",
        },
        headers=_service_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_identity_link_requires_verified_claim(client, session, account, sample_candidates, monkeypatch):
    _enable_bridge(monkeypatch)
    _candidate(session, account, sample_candidates)

    response = client.post(
        "/v1/internal/openinstinct/identity-links",
        json={"external_user_id": "better-auth:openinstinct-user", "email": "ada@example.com"},
        headers=_service_headers(identities=[]),
    )
    assert response.status_code == 403


def test_task_submits_without_second_approval_and_cannot_duplicate(
    client, session, account, sample_candidates, monkeypatch
):
    _enable_bridge(monkeypatch)
    candidate = _candidate(session, account, sample_candidates)
    posting = _posting(session, account["org_id"])
    _link(client)

    payload = {"external_task_id": "candidate-said-apply-1", "job_posting_id": posting.id}
    response = client.post(
        "/v1/internal/openinstinct/application-tasks",
        json=payload,
        headers=_service_headers(),
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]

    application = session.query(Application).filter_by(candidate_id=candidate.id).one()
    transition(session, application, to_status="prepared", actor="package-worker")
    session.commit()
    ready = client.get(
        f"/v1/internal/openinstinct/application-tasks/{task_id}", headers=_service_headers()
    )
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"

    result = client.post(
        f"/v1/internal/openinstinct/application-tasks/{task_id}/result",
        json={"status": "submitted", "external_id": "ats-confirmation-1"},
        headers=_service_headers(),
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "submitted"
    session.expire_all()
    assert session.query(ApplicationSubmission).one().status == "succeeded"
    assert session.get(Application, application.id).status == "submitted"

    duplicate = client.post(
        "/v1/internal/openinstinct/application-tasks",
        json={"external_task_id": "candidate-said-apply-2", "job_posting_id": posting.id},
        headers=_service_headers(),
    )
    assert duplicate.status_code == 409


def test_job_feed_returns_only_linked_candidates_actionable_matches(
    client, session, account, sample_candidates, monkeypatch
):
    _enable_bridge(monkeypatch)
    candidate = _candidate(session, account, sample_candidates)
    posting = _matched_posting(session, candidate)
    _link(client)

    response = client.get("/v1/internal/openinstinct/job-feed", headers=_service_headers())

    assert response.status_code == 200, response.text
    cards = response.json()["cards"]
    assert cards[0]["posting_id"] == posting.id
    assert cards[0]["title"] == "Machine Learning Engineer"
    assert "recruiter_only" not in cards[0]


def test_task_cannot_cross_org_and_recruiter_timeline_keeps_voice_plus_openinstinct(
    client, session, account, sample_candidates, monkeypatch
):
    _enable_bridge(monkeypatch)
    candidate = _candidate(session, account, sample_candidates)
    _link(client)
    rival_signup = client.post(
        "/v1/auth/signup",
        json={
            "org_name": "Other Talent",
            "email": "other-recruiter@example.com",
            "password": "supersecret1",
            "name": "Other Recruiter",
        },
    )
    assert rival_signup.status_code == 201
    foreign = _posting(session, rival_signup.json()["user"]["org_id"])

    response = client.post(
        "/v1/internal/openinstinct/application-tasks",
        json={"external_task_id": "cross-org", "job_posting_id": foreign.id},
        headers=_service_headers(),
    )
    assert response.status_code == 404

    own_posting = _posting(session, account["org_id"])
    created = client.post(
        "/v1/internal/openinstinct/application-tasks",
        json={"external_task_id": "timeline-task", "job_posting_id": own_posting.id},
        headers=_service_headers(),
    )
    assert created.status_code == 202, created.text
    task = session.get(OpenInstinctApplicationTask, created.json()["id"])
    assert task is not None
    task.status = "needs_human"
    task.result = {"error": "A required question needs the candidate."}
    session.commit()
    with patch(
        "juicebox.routers.candidates.fetch_candidate_conversations",
        return_value={
            "available": True,
            "conversations": [
                {
                    "id": "linq:thread-1",
                    "channel": "linq",
                    "updated_at": "2026-08-27T12:00:00Z",
                    "messages": [{"direction": "inbound", "body": "Please apply.", "created_at": "2026-08-27T11:59:00Z"}],
                }
            ],
        },
    ):
        timeline = client.get(f"/v1/candidates/{candidate.id}/timeline", headers=account["headers"])
    assert timeline.status_code == 200, timeline.text
    body = timeline.json()
    assert body["openinstinct_available"] is True
    assert body["openinstinct_conversations"][0]["channel"] == "linq"
    assert body["openinstinct_application_tasks"][0]["status"] == "needs_human"
