"""Ingest without destroying data: promote rules, the paid-fetch cache, the
upload link.

The promote tests are the §7 contract: a re-ingest after a human correction
must never delete an experience, and a parsed summary must never displace one
the search haystack already serves.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox.enrich.linkedin_profile import (
    FakeProfileProvider,
    ProfileProviderUnavailable,
    find_linkedin,
    linkedin_slug,
    map_profile,
    set_profile_provider,
)
from juicebox.enrich.promote import merged_payload, payload_from_resume_v1, promote
from juicebox.jobs import KIND_JOBS_MATCH_ONE, KIND_PROFILE_INGEST, enqueue
from juicebox.messaging.channels import WEB, FakeChannel, set_channel
from juicebox.messaging.threads import open_thread
from juicebox.messaging.uploads import mint_upload_link
from juicebox.models import Candidate, CandidateDocument, Job, utcnow
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
            },
            {
                "companyName": "Stripe",
                "title": "Machine Learning Engineer",
                "startEndDate": {"start": {"year": 2016}, "end": {"year": 2018, "month": 12}},
            },
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
def existing(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    return session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()


class TestUrlHandling:
    def test_find_and_canonicalise(self):
        assert (
            find_linkedin("here you go https://uk.linkedin.com/in/ada-okonkwo/ thanks")
            == "https://www.linkedin.com/in/ada-okonkwo"
        )
        assert find_linkedin("no url here") == ""
        assert linkedin_slug("linkedin.com/in/Ada-Okonkwo") == "ada-okonkwo"


class TestMapProfile:
    def test_vendor_payload_maps_to_upsert_shape(self):
        parsed = map_profile(SCRAPIN_PAYLOAD, linkedin_url=ADA_URL)
        assert parsed["source"] == "linkedin_ingest"
        assert parsed["source_id"] == "ada-okonkwo"
        assert parsed["full_name"] == "Ada Okonkwo"
        assert parsed["experiences"][0]["company"] == "Modal"
        assert parsed["experiences"][0]["start_date"] == "2019-01"
        assert parsed["experiences"][0]["is_current"] is True
        assert parsed["experiences"][1]["end_date"] == "2018-12"
        assert parsed["educations"][0]["school"] == "Stanford University"
        assert parsed["educations"][0]["end_year"] == 2016


class TestPromote:
    def test_reingest_never_wipes_experiences(self, session, account, existing):
        """The _replace_children hazard: a parse knowing only ONE job must not
        cost the candidate the rest of their history."""
        sparse = {
            "source": "linkedin_ingest",
            "source_id": "ada-okonkwo",
            "full_name": "Ada Okonkwo",
            "experiences": [
                {"company": "Modal", "title": "Senior Machine Learning Engineer",
                 "start_date": "2019-01", "is_current": True},
            ],
        }
        before = {exp.company for exp in existing.experiences}
        assert "Stripe" in before
        promote(session, org_id=account["org_id"], parsed=sparse, candidate=existing)
        session.expire(existing)
        after = {exp.company for exp in existing.experiences}
        assert before <= after  # nothing lost

    def test_new_facts_are_added(self, session, account, existing):
        parsed = {
            "source": "linkedin_ingest",
            "source_id": "ada-okonkwo",
            "experiences": [
                {"company": "DeepMind", "title": "Research Engineer",
                 "start_date": "2013-01", "end_date": "2015-12"},
            ],
            "educations": [{"school": "MIT", "degree": "BS"}],
        }
        result = promote(session, org_id=account["org_id"], parsed=parsed, candidate=existing)
        assert result.added_experiences == 1
        assert result.added_educations == 1
        session.expire(existing)
        assert "DeepMind" in {exp.company for exp in existing.experiences}
        assert "MIT" in {edu.school for edu in existing.educations}

    def test_summary_and_skills_never_move_onto_a_filled_row(self, session, account, existing):
        """§7: those two fields feed the search haystack."""
        existing.summary = "A summary a recruiter wrote."
        session.flush()
        parsed = {
            "source": "linkedin_ingest",
            "source_id": "ada-okonkwo",
            "summary": "A parsed summary that must not land.",
            "skills": ["Cobol"],
        }
        payload, _ = merged_payload(existing, parsed)
        if payload is not None:
            assert payload["summary"] == "A summary a recruiter wrote."
            assert "Cobol" not in (payload.get("skills") or [])

    def test_nothing_new_means_no_write(self, session, account, existing):
        parsed = {
            "source": "linkedin_ingest",
            "source_id": "ada-okonkwo",
            "full_name": existing.full_name,
            "experiences": [],
        }
        payload, _ = merged_payload(existing, parsed)
        assert payload is None

    def test_resume_v1_converts(self):
        parsed = payload_from_resume_v1(
            {
                "contact": {"name": "Bo Lindqvist", "email": "bo@example.com", "phone": "",
                            "location": "Berlin", "linkedin_url": ""},
                "summary": "Frontend engineer.",
                "experience": [
                    {"company": "Linear", "title": "Senior Frontend Engineer",
                     "start": "2020-06", "end": "present", "bullets": ["Owned the library"]},
                ],
                "education": [],
                "skills": ["TypeScript"],
                "certifications": [],
            }
        )
        assert parsed["experiences"][0]["is_current"] is True
        assert parsed["emails"] == ["bo@example.com"]
        assert parsed["skills"] == ["TypeScript"]


class TestIngestJob:
    def _enqueue(self, session, org_id, **payload):
        job = enqueue(
            session,
            org_id=org_id,
            kind=KIND_PROFILE_INGEST,
            idempotency_key=f"{KIND_PROFILE_INGEST}:test:{payload.get('linkedin_url')}",
            payload=payload,
        )
        job.status = "running"
        return job

    def test_fetch_promotes_and_enqueues_match(self, session, account):
        set_profile_provider(FakeProfileProvider({ADA_URL: SCRAPIN_PAYLOAD}))
        job = self._enqueue(session, account["org_id"], linkedin_url=ADA_URL)
        process_job(session, job)
        assert job.status == "succeeded", job.error

        candidate = session.scalar(
            select(Candidate).where(Candidate.source_id == "ada-okonkwo")
        )
        assert candidate is not None
        assert candidate.profile_ingest_checked_at is not None
        assert {exp.company for exp in candidate.experiences} == {"Modal", "Stripe"}

        # The parse artifact is kept for review and re-promotion.
        artifact = session.scalar(
            select(CandidateDocument).where(CandidateDocument.candidate_id == candidate.id)
        )
        assert artifact is not None and artifact.source == "linkedin_ingest"

        match_job = session.scalar(select(Job).where(Job.kind == KIND_JOBS_MATCH_ONE))
        assert match_job is not None

    def test_not_found_stamps_the_cache(self, session, account, existing):
        set_profile_provider(FakeProfileProvider({}))
        job = self._enqueue(
            session, account["org_id"], linkedin_url=ADA_URL, candidate_id=existing.id
        )
        process_job(session, job)
        assert job.status == "succeeded"
        assert existing.profile_ingest_checked_at is not None
        assert "not found" in existing.profile_ingest_error

    def test_provider_outage_never_stamps(self, session, account, existing):
        provider = FakeProfileProvider({ADA_URL: SCRAPIN_PAYLOAD})
        provider.fail_next = ProfileProviderUnavailable("quota wall")
        set_profile_provider(provider)
        job = self._enqueue(
            session, account["org_id"], linkedin_url=ADA_URL, candidate_id=existing.id
        )
        process_job(session, job)
        # Failed for retry, and the cache is unstamped -- the ContactOut lesson.
        assert job.status == "queued" and job.attempts == 1
        assert existing.profile_ingest_checked_at is None

    def test_fresh_check_is_not_repaid(self, session, account, existing):
        existing.profile_ingest_checked_at = utcnow() - timedelta(days=1)
        session.flush()
        provider = FakeProfileProvider({ADA_URL: SCRAPIN_PAYLOAD})
        set_profile_provider(provider)
        job = self._enqueue(
            session, account["org_id"], linkedin_url=ADA_URL, candidate_id=existing.id
        )
        process_job(session, job)
        assert job.status == "succeeded"
        assert provider.calls == []  # no money spent


class TestUploadLink:
    @pytest.fixture
    def thread(self, session, account):
        set_channel(WEB, FakeChannel())
        return open_thread(
            session,
            org_id=account["org_id"],
            channel=WEB,
            consent_messaging=True,
            contact_name="Ada Okonkwo",
            contact_email="ada-upload@example.com",
        )

    def _pdf(self) -> bytes:
        from juicebox.docgen.render import render_letter_pdf

        return render_letter_pdf("A resume, allegedly.", candidate_name="Ada")

    def test_link_round_trip(self, client, session, account, thread):
        session.commit()
        link = mint_upload_link(thread)
        assert f"/v1/intake/uploads/{thread.id}?token=" in link
        path = link.split("http://testserver", 1)[-1]

        page = client.get(path)
        assert page.status_code == 200
        assert "Send your resume" in page.text

        response = client.post(
            path, files={"file": ("cv.pdf", self._pdf(), "application/pdf")}
        )
        assert response.status_code == 201, response.text

        doc = session.scalar(
            select(CandidateDocument).where(CandidateDocument.thread_id == thread.id)
        )
        assert doc is not None
        assert doc.kind == "resume_uploaded" and doc.is_default
        assert doc.candidate_id  # link_candidate ran
        parse_job = session.scalar(select(Job).where(Job.kind == "docs.parse"))
        assert parse_job is not None

    def test_wrong_token_is_401(self, client, session, account, thread):
        session.commit()
        response = client.get(f"/v1/intake/uploads/{thread.id}?token=forged")
        assert response.status_code == 401
        response = client.post(
            f"/v1/intake/uploads/{thread.id}?token=forged",
            files={"file": ("cv.pdf", self._pdf(), "application/pdf")},
        )
        assert response.status_code == 401

    def test_unlinkable_thread_is_409(self, client, session, account):
        set_channel(WEB, FakeChannel())
        bare = open_thread(session, org_id=account["org_id"], channel=WEB)
        session.commit()
        link = mint_upload_link(bare)
        path = link.split("http://testserver", 1)[-1]
        response = client.post(
            path, files={"file": ("cv.pdf", self._pdf(), "application/pdf")}
        )
        assert response.status_code == 409
