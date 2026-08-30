"""Contract coverage for the separate OpenInstinct candidate deployment."""

from __future__ import annotations

from unittest.mock import patch

from juicebox.applications.state import transition
from juicebox.config import reset_settings_cache
from juicebox.jobs import KIND_JOBS_DISCOVER_ONE
from juicebox.models import (
    Application,
    ApplicationSubmission,
    Candidate,
    CandidateDocument,
    CandidatePreference,
    IntakeMessage,
    IntakeThread,
    Job,
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


def _event_payload(*, conversation_id="linq:foray-1", provider_native_id="carrier-1", direction="inbound"):
    return {
        "conversation_id": conversation_id,
        "provider_native_id": provider_native_id,
        "direction": direction,
        "body": "please apply to this role",
        "channel": "linq",
        "display_name": "New Candidate",
    }


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


def test_identity_link_adopts_a_web_account_to_its_phone_workspace(
    client, session, account, sample_candidates, monkeypatch
):
    _enable_bridge(monkeypatch)
    candidate = _candidate(session, account, sample_candidates)
    _link(client)

    response = client.post(
        "/v1/internal/openinstinct/identity-links",
        json={
            "external_user_id": "phone:workspace-digest",
            "email": "ada@example.com",
            "phone": "+14155550123",
        },
        headers=_service_headers(
            subject="phone:workspace-digest",
            identities=[
                {"kind": "email", "value": "ada@example.com", "verified": True},
                {"kind": "phone", "value": "+14155550123", "verified": True},
            ],
        ),
    )

    assert response.status_code == 201, response.text
    assert response.json()["candidate_id"] == candidate.id


def test_conversation_event_creates_candidate_and_is_idempotent(
    client, session, account, monkeypatch
):
    _enable_bridge(monkeypatch)
    monkeypatch.setenv("OPENINSTINCT_DEFAULT_ORG_ID", account["org_id"])
    reset_settings_cache()
    identities = [
        {"kind": "email", "value": "new-candidate@example.com", "verified": True},
        {"kind": "phone", "value": "+14155550124", "verified": True},
    ]
    headers = _service_headers(subject="better-auth:new-candidate", identities=identities)

    created = client.post(
        "/v1/internal/openinstinct/conversation-events",
        json=_event_payload(),
        headers=headers,
    )
    assert created.status_code == 202, created.text
    assert created.json()["created"] is True
    duplicate = client.post(
        "/v1/internal/openinstinct/conversation-events",
        json=_event_payload(),
        headers=headers,
    )
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["created"] is False

    thread = session.get(IntakeThread, created.json()["thread_id"])
    assert thread is not None
    assert thread.channel == "openinstinct"
    assert thread.mode == "managing"
    assert thread.entry_point == "openinstinct"
    assert thread.review_needed is False
    candidate = session.get(Candidate, thread.candidate_id)
    assert candidate is not None
    assert candidate.emails == ["new-candidate@example.com"]
    assert candidate.phones == ["+14155550124"]
    assert session.query(IntakeMessage).filter_by(thread_id=thread.id).count() == 1


def test_conversation_event_fails_closed_without_a_default_org(client, monkeypatch):
    _enable_bridge(monkeypatch)
    response = client.post(
        "/v1/internal/openinstinct/conversation-events",
        json=_event_payload(),
        headers=_service_headers(
            subject="better-auth:unconfigured-candidate",
            identities=[
                {
                    "kind": "email",
                    "value": "unconfigured-candidate@example.com",
                    "verified": True,
                }
            ],
        ),
    )
    assert response.status_code == 422
    assert "default organization" in response.json()["detail"]


def test_mirrored_thread_suppresses_legacy_openinstinct_pull(
    client, session, account, sample_candidates, monkeypatch
):
    _enable_bridge(monkeypatch)
    candidate = _candidate(session, account, sample_candidates)
    _link(client)
    response = client.post(
        "/v1/internal/openinstinct/conversation-events",
        json=_event_payload(),
        headers=_service_headers(),
    )
    assert response.status_code == 202, response.text

    with patch(
        "juicebox.routers.candidates.fetch_candidate_conversations",
        return_value={"available": True, "conversations": [{"id": "legacy"}]},
    ):
        timeline = client.get(
            f"/v1/candidates/{candidate.id}/timeline", headers=account["headers"]
        )
    assert timeline.status_code == 200, timeline.text
    assert timeline.json()["openinstinct_conversations"] == []


def test_linked_candidate_can_upload_a_protected_resume(client, session, account, sample_candidates, monkeypatch):
    _enable_bridge(monkeypatch)
    candidate = _candidate(session, account, sample_candidates)
    _link(client)

    response = client.post(
        "/v1/internal/openinstinct/resumes",
        files={"file": ("ada.pdf", b"%PDF-1.4\nresume", "application/pdf")},
        headers=_service_headers(),
    )

    assert response.status_code == 201, response.text
    document = session.get(CandidateDocument, response.json()["id"])
    assert document is not None
    assert document.candidate_id == candidate.id
    assert document.is_default is True
    assert document.protected_default is True
    assert document.status == "uploaded"


def test_default_resume_distinguishes_missing_from_pending_parse(
    client, session, account, sample_candidates, monkeypatch
):
    _enable_bridge(monkeypatch)
    _candidate(session, account, sample_candidates)
    _link(client)

    missing = client.get(
        "/v1/internal/openinstinct/resumes/default", headers=_service_headers()
    )
    assert missing.status_code == 404
    assert "Attach a PDF or DOCX" in missing.json()["detail"]

    uploaded = client.post(
        "/v1/internal/openinstinct/resumes",
        files={"file": ("ada.pdf", b"%PDF-1.4\nresume", "application/pdf")},
        headers=_service_headers(),
    )
    assert uploaded.status_code == 201, uploaded.text

    pending = client.get(
        "/v1/internal/openinstinct/resumes/default", headers=_service_headers()
    )
    assert pending.status_code == 409
    assert "still being parsed" in pending.json()["detail"]


def test_openinstinct_resume_rejects_non_resume_bytes(
    client, session, account, sample_candidates, monkeypatch
):
    _enable_bridge(monkeypatch)
    _candidate(session, account, sample_candidates)
    _link(client)

    response = client.post(
        "/v1/internal/openinstinct/resumes",
        files={"file": ("not-a-resume.pdf", b"not a PDF", "application/pdf")},
        headers=_service_headers(),
    )

    assert response.status_code == 415


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
    assert response.json()["searching"] is False

    card = client.get(
        f"/v1/internal/openinstinct/job-cards/{posting.id}?index=1&total=5",
        headers=_service_headers(),
    )
    assert card.status_code == 200, card.text
    assert card.headers["content-type"] == "image/png"
    assert card.content.startswith(b"\x89PNG")


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


def test_empty_job_feed_queues_the_same_exa_search_the_messaging_bot_uses(
    client, session, account, sample_candidates, monkeypatch
):
    _enable_bridge(monkeypatch)
    candidate = _candidate(session, account, sample_candidates)
    session.add(
        CandidatePreference(
            org_id=candidate.org_id,
            candidate_id=candidate.id,
            spec={"job_titles": ["platform engineer"], "locations": ["Remote"]},
        )
    )
    session.commit()
    _link(client)

    response = client.get(
        "/v1/internal/openinstinct/job-feed?q=rust&location=NYC",
        headers=_service_headers(),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cards"] == []
    assert payload["searching"] is True
    assert payload["discovery"]["state"] == "queued"
    assert payload["discovery"]["job_id"]
    job = session.query(Job).filter_by(kind=KIND_JOBS_DISCOVER_ONE).one()
    assert job.payload["candidate_id"] == candidate.id
    assert "rust jobs in NYC" in job.payload["queries"]


def test_empty_job_feed_without_a_spec_or_query_does_not_spend_exa(
    client, session, account, sample_candidates, monkeypatch
):
    _enable_bridge(monkeypatch)
    _candidate(session, account, sample_candidates)
    _link(client)

    response = client.get("/v1/internal/openinstinct/job-feed", headers=_service_headers())
    assert response.status_code == 200, response.text
    assert response.json() == {
        "cards": [],
        "searching": False,
        "discovery": {
            "state": "empty",
            "detail": "Add a target role or search query before looking for openings.",
        },
    }
    assert session.query(Job).filter_by(kind=KIND_JOBS_DISCOVER_ONE).count() == 0

