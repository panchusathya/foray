"""Dropping a LinkedIn (or a resume) earns you an actual document.

The promise: the moment the CRM knows enough about a person, ONE profile-level
ATS resume exists — viewable and downloadable in the portal before any
application does. Regenerations update that row in place; failures are written
onto it honestly rather than raised into a retry loop; and a candidate the CRM
knows nothing real about gets no document at all, because a name-and-headline
PDF asserted under somebody's name helps nobody.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.docgen.profile import SKIPPED_EMPTY, profile_resume_of, refresh_profile_resume
from juicebox.enrich.linkedin_profile import FakeProfileProvider, set_profile_provider
from juicebox.jobs import KIND_PROFILE_INGEST, KIND_PROFILE_RESUME, enqueue
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.identity import link_candidate
from juicebox.messaging.linkedin_flow import FLOW_LINKEDIN_SHORT
from juicebox.messaging.threads import open_thread
from juicebox.models import Candidate, CandidateDocument, Job
from juicebox.repository import upsert_candidates
from juicebox.worker import process_job

ADA_URL = "https://www.linkedin.com/in/ada-okonkwo"

SCRAPIN_PAYLOAD = {
    "person": {
        "fullName": "Ada Okonkwo",
        "headline": "ML infrastructure",
        "summary": "Builds inference platforms.",
        "location": "San Francisco",
        "skills": ["Python", "Kubernetes"],
        "positions": [
            {
                "companyName": "Modal",
                "title": "Senior Machine Learning Engineer",
                "startEndDate": {"start": {"year": 2019, "month": 1}},
            }
        ],
        "schools": [
            {
                "schoolName": "Stanford University",
                "degreeName": "MS",
                "fieldOfStudy": "Computer Science",
                "startEndDate": {"start": {"year": 2014}, "end": {"year": 2016}},
            }
        ],
    }
}


@pytest.fixture
def rich_candidate(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    return session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()


def _bare_candidate(session, org_id, **extra) -> Candidate:
    """The shape a LinkedIn drop creates: scalars only, no children."""
    upsert_candidates(
        session,
        org_id=org_id,
        payloads=[
            {
                "source": "intake_thread",
                "source_id": "bare@example.com",
                "full_name": "Bare Candidate",
                "emails": ["bare@example.com"],
                **extra,
            }
        ],
    )
    return session.query(Candidate).filter_by(full_name="Bare Candidate").one()


class TestRefresh:
    def test_a_real_candidate_gets_a_ready_document(self, session, account, rich_candidate):
        doc, error = refresh_profile_resume(session, rich_candidate)
        assert error == ""
        assert doc.status == "ready"
        assert doc.kind == "resume_generated"
        assert doc.application_id is None
        assert doc.size_bytes > 0
        assert doc.structured["experience"], "the history made it into the JSON"
        from juicebox import storage

        assert storage.read_bytes(doc.storage_path).startswith(b"%PDF-")

    def test_regeneration_updates_the_same_row(self, session, account, rich_candidate):
        first, _ = refresh_profile_resume(session, rich_candidate)
        rich_candidate.summary = "Now with a fresh summary."
        second, _ = refresh_profile_resume(session, rich_candidate)
        assert second.id == first.id, "one profile resume per candidate, forever"
        rows = session.scalars(
            select(CandidateDocument).where(
                CandidateDocument.candidate_id == rich_candidate.id,
                CandidateDocument.kind == "resume_generated",
            )
        ).all()
        assert len(rows) == 1

    def test_a_candidate_the_crm_knows_nothing_about_gets_no_document(self, session, account):
        candidate = _bare_candidate(session, account["org_id"])
        doc, reason = refresh_profile_resume(session, candidate)
        assert doc is None
        assert reason == SKIPPED_EMPTY
        assert profile_resume_of(session, candidate.id) is None

    def test_a_linkedin_only_candidate_buys_the_history_first(self, session, account):
        """The build_package rule, moved to the moment the profile settles:
        an ATS resume with a blank career is not the promised artifact."""
        candidate = _bare_candidate(session, account["org_id"], linkedin_url=ADA_URL)
        set_profile_provider(FakeProfileProvider({ADA_URL: SCRAPIN_PAYLOAD}))
        try:
            doc, error = refresh_profile_resume(session, candidate)
        finally:
            set_profile_provider(None)
        assert error == ""
        companies = {job["company"] for job in doc.structured["experience"]}
        assert "Modal" in companies


class TestWorkerWiring:
    def test_profile_ingest_refreshes_the_resume(self, session, account):
        set_profile_provider(FakeProfileProvider({ADA_URL: SCRAPIN_PAYLOAD}))
        try:
            enqueue(
                session,
                org_id=account["org_id"],
                kind=KIND_PROFILE_INGEST,
                idempotency_key="ingest-test",
                payload={"linkedin_url": ADA_URL},
            )
            for _ in range(6):
                job = session.scalar(select(Job).where(Job.status == "queued"))
                if job is None:
                    break
                process_job(session, job)
                session.flush()
        finally:
            set_profile_provider(None)

        candidate = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        doc = profile_resume_of(session, candidate.id)
        assert doc is not None and doc.status == "ready"

    def test_linking_a_thread_asks_for_one_build(self, session, account):
        set_channel(LINQ, FakeChannel(consent=True))
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat-pr",
            entry_point="linq_inbound",
            from_address="+14155550177",
            consent_messaging=True,
            flow=FLOW_LINKEDIN_SHORT,
        )
        thread.contact_email = "bare@example.com"
        candidate_id = link_candidate(session, thread=thread)
        assert candidate_id
        job = session.scalar(select(Job).where(Job.kind == KIND_PROFILE_RESUME))
        assert job is not None
        assert job.payload["candidate_id"] == candidate_id
        # Called again (it runs on many turns) — still exactly one job.
        link_candidate(session, thread=thread)
        jobs = session.scalars(select(Job).where(Job.kind == KIND_PROFILE_RESUME)).all()
        assert len(jobs) == 1

    def test_a_failed_build_lands_on_the_row_not_the_queue(
        self, session, account, rich_candidate, monkeypatch
    ):
        monkeypatch.setattr(
            "juicebox.docgen.profile.build_resume_json",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("renderer exploded")),
        )
        enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_PROFILE_RESUME,
            idempotency_key="fail-test",
            payload={"candidate_id": rich_candidate.id},
        )
        job = session.scalar(select(Job).where(Job.kind == KIND_PROFILE_RESUME))
        process_job(session, job)
        assert job.status == "succeeded", "recorded, not retried"
        doc = profile_resume_of(session, rich_candidate.id)
        assert doc.status == "failed"
        assert "renderer exploded" in doc.error


class TestPortalCanSeeIt:
    def test_listed_viewable_and_downloadable(self, client, session, account, rich_candidate):
        doc, _ = refresh_profile_resume(session, rich_candidate)
        session.commit()

        # The portal session flow needs a magic link; sign the document URL
        # directly the way the download-url endpoint does, which is the part
        # under test — the list endpoint is covered by portal API tests.
        import time as _time

        from juicebox.routers.portal import PURPOSE_DOC_SIG
        from juicebox.security import signed_intake_token

        expires = int(_time.time()) + 60
        sig = signed_intake_token(PURPOSE_DOC_SIG, f"{doc.id}:{expires}")
        inline = client.get(f"/v1/portal/documents/{doc.id}/file?exp={expires}&sig={sig}")
        assert inline.status_code == 200, inline.text
        assert inline.headers["content-disposition"].startswith("inline")
        assert inline.content.startswith(b"%PDF-")

        attach = client.get(
            f"/v1/portal/documents/{doc.id}/file?exp={expires}&sig={sig}&download=true"
        )
        assert attach.status_code == 200
        assert attach.headers["content-disposition"].startswith("attachment")


class TestSkillCasing:
    def test_the_taxonomy_lowercase_is_repaired_for_the_page(self):
        from juicebox.docgen.render import display_skill

        assert display_skill("python") == "Python"
        assert display_skill("pytorch") == "PyTorch"
        assert display_skill("aws") == "AWS"
        assert display_skill("distributed training") == "Distributed training"
        assert display_skill("llm inference") == "LLM inference"

    def test_the_authors_own_casing_always_wins(self):
        from juicebox.docgen.render import display_skill

        assert display_skill("golang (dabbling)") == "Golang (dabbling)"
        assert display_skill("scikit-learn") == "Scikit-learn"
        assert display_skill("LaTeX") == "LaTeX"
        assert display_skill("iOS accessibility") == "iOS accessibility"
