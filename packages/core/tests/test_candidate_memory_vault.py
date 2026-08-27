"""Regression coverage for candidate memory and secret custody."""

from __future__ import annotations

from sqlalchemy import select

from juicebox.applications.candidate_credentials import read_password, revoke, save_password
from juicebox.config import reset_settings_cache
from juicebox.intake.facts import sync_preference_facts
from juicebox.messaging.attachments import store_thread_resume
from juicebox.messaging.threads import open_thread
from juicebox.models import (
    Candidate,
    CandidateDocument,
    CandidatePreference,
    CandidatePreferenceFact,
)


def _candidate(session, org_id: str) -> Candidate:
    candidate = Candidate(
        org_id=org_id,
        source="test",
        source_id="candidate-memory-vault",
        full_name="Ada Lovelace",
        first_name="Ada",
        emails=["ada@example.com"],
    )
    session.add(candidate)
    session.flush()
    return candidate


def test_returning_text_thread_starts_from_stored_preferences(session, account):
    candidate = _candidate(session, account["org_id"])
    session.add(
        CandidatePreference(
            org_id=account["org_id"], candidate_id=candidate.id, work_authorization="US citizen"
        )
    )
    session.flush()

    thread = open_thread(session, org_id=account["org_id"], candidate_id=candidate.id)

    assert thread.record["work_authorization"] == "US citizen"


def test_connector_resume_does_not_replace_candidate_protected_default(session, account):
    candidate = _candidate(session, account["org_id"])
    thread = open_thread(session, org_id=account["org_id"], candidate_id=candidate.id)
    chosen = CandidateDocument(
        org_id=account["org_id"], candidate_id=candidate.id, kind="resume_uploaded",
        filename="chosen.pdf", content_type="application/pdf", storage_path="chosen.pdf",
        is_default=True, protected_default=True,
    )
    session.add(chosen)
    session.flush()

    imported = store_thread_resume(
        session, thread=thread, candidate_id=candidate.id, data=b"%PDF-1.4\nresume",
        filename="imported.pdf", content_type="application/pdf", source="connector",
    )
    session.expire_all()

    assert session.get(CandidateDocument, chosen.id).is_default is True
    assert session.get(CandidateDocument, imported.id).is_default is False


def test_fact_metadata_tracks_direct_candidate_statement(session, account):
    candidate = _candidate(session, account["org_id"])
    thread = open_thread(session, org_id=account["org_id"], candidate_id=candidate.id)
    preference = CandidatePreference(org_id=account["org_id"], candidate_id=candidate.id)
    session.add(preference)
    session.flush()

    sync_preference_facts(
        session,
        preference=preference,
        previous={"work_authorization": ""},
        current={"work_authorization": "US citizen", "declined": []},
        source_thread_id=thread.id,
        direct_candidate_statement=True,
    )

    fact = session.scalar(select(CandidatePreferenceFact))
    assert fact.field_key == "work_authorization"
    assert fact.direct_candidate_statement is True
    assert fact.confirmed_at is None


def test_vault_revoke_and_key_rotation(session, account, monkeypatch):
    candidate = _candidate(session, account["org_id"])
    row = save_password(
        session, org_id=account["org_id"], candidate_id=candidate.id, vendor="workday",
        tenant="acme", username="ada@example.com", password="correct horse battery staple",
        origin="candidate_supplied",
    )
    assert read_password(session, candidate_id=candidate.id, vendor="workday", tenant="acme") == (
        "ada@example.com", "correct horse battery staple",
    )

    from juicebox.integrations.crypto import decrypt_json, encrypt_json

    monkeypatch.setenv("SECRET_KEY", "old-secret")
    monkeypatch.delenv("SECRET_KEY_PREVIOUS", raising=False)
    reset_settings_cache()
    legacy = encrypt_json({"password": "old"})
    monkeypatch.setenv("SECRET_KEY", "new-secret")
    monkeypatch.setenv("SECRET_KEY_PREVIOUS", "old-secret")
    reset_settings_cache()
    assert decrypt_json(legacy) == {"password": "old"}

    revoke(session, row)
    assert read_password(session, candidate_id=candidate.id, vendor="workday", tenant="acme") == ("", "")
