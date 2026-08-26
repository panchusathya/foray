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
    def test_an_uploaded_resume_is_returned_without_generating_a_replacement(
        self, session, account, rich_candidate
    ):
        from juicebox import storage

        uploaded = CandidateDocument(
            org_id=account["org_id"],
            candidate_id=rich_candidate.id,
            kind="resume_uploaded",
            is_default=True,
            filename="my-resume.pdf",
            content_type="application/pdf",
            status="ready",
            source="upload",
            structured={"contact": {"name": "Ada"}, "experience": []},
        )
        session.add(uploaded)
        session.flush()
        uploaded.storage_path = storage.document_relpath(
            org_id=uploaded.org_id,
            candidate_id=uploaded.candidate_id,
            document_id=uploaded.id,
        )
        storage.save_bytes(uploaded.storage_path, b"%PDF-candidate-original")

        doc, error = refresh_profile_resume(session, rich_candidate)

        assert error == ""
        assert doc.id == uploaded.id
        assert storage.read_bytes(doc.storage_path) == b"%PDF-candidate-original"
        assert profile_resume_of(session, rich_candidate.id) is None

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

    def test_linkedin_bullet_markers_survive_as_separate_bullets(
        self, session, account, rich_candidate
    ):
        rich_candidate.experiences[0].description = "First result • Second result\nThird result"
        doc, error = refresh_profile_resume(session, rich_candidate)
        assert error == ""
        assert doc.structured["experience"][0]["bullets"] == [
            "First result",
            "Second result",
            "Third result",
        ]

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


class TestAdaptiveLayout:
    """Section order flexes with the person, the way career centers teach it."""

    TODAY = __import__("datetime").date(2026, 8, 25)

    def _structured(self, *, experience, education):
        return {"experience": experience, "education": education}

    def test_a_working_professional_leads_with_experience(self):
        from juicebox.docgen.render import _education_first

        structured = self._structured(
            experience=[{"start": "2016-07", "end": "present"}],
            education=[{"end_year": "2016"}],
        )
        assert _education_first(structured, self.TODAY) is False

    def test_the_header_is_centered_while_content_stays_left_aligned(self):
        from reportlab.lib.enums import TA_CENTER, TA_LEFT

        from juicebox.docgen.render import _BODY, _CONTACT, _NAME

        assert _NAME.alignment == TA_CENTER
        assert _CONTACT.alignment == TA_CENTER
        assert _BODY.alignment == TA_LEFT
        assert _NAME.fontName == "Times-Bold"
        assert _CONTACT.fontName == "Times-Roman"
        assert _BODY.fontName == "Times-Roman"

    def test_email_and_linkedin_are_real_pdf_links(self):
        import io

        from pypdf import PdfReader

        from juicebox.docgen.render import render_resume_pdf

        data = render_resume_pdf(
            {
                "contact": {
                    "name": "Priya Raman",
                    "email": "priya@example.com",
                    "linkedin_url": "linkedin.com/in/priya-raman",
                }
            }
        )
        page = PdfReader(io.BytesIO(data)).pages[0]
        uris = {
            str(annotation.get_object()["/A"]["/URI"])
            for annotation in page.get("/Annots", [])
        }
        assert "mailto:priya@example.com" in uris
        assert "https://linkedin.com/in/priya-raman" in uris

    def test_two_column_rows_fit_the_same_frame_as_section_rules(self):
        from reportlab.platypus import Paragraph

        from juicebox.docgen.render import _CONTENT_WIDTH, _ROLE, _two_columns

        row = _two_columns(Paragraph("Company", _ROLE), Paragraph("Location", _ROLE))
        assert sum(row._argW) == _CONTENT_WIDTH
        assert row.hAlign == "LEFT"

    def test_a_student_leads_with_education(self):
        from juicebox.docgen.render import _education_first

        structured = self._structured(
            # Two summer internships can SPAN two years; the future
            # graduation year is what marks a student.
            experience=[
                {"start": "2025-06", "end": "2025-08"},
                {"start": "2026-06", "end": "2026-08"},
            ],
            education=[{"end_year": "2027"}],
        )
        assert _education_first(structured, self.TODAY) is True

    def test_no_experience_at_all_leads_with_education(self):
        from juicebox.docgen.render import _education_first

        assert _education_first(self._structured(experience=[], education=[{}]), self.TODAY)

    def test_an_executive_program_does_not_make_a_student(self):
        from juicebox.docgen.render import _education_first

        structured = self._structured(
            experience=[{"start": "2014-01", "end": "present"}],
            education=[{"end_year": "2027"}],  # an EMBA in progress
        )
        assert _education_first(structured, self.TODAY) is False

    def test_the_page_takes_every_new_section_in_stride(self):
        from juicebox.docgen.render import render_resume_pdf

        data = render_resume_pdf(
            {
                "contact": {"name": "Jordan Lee", "email": "j@l.dev", "phone": "555"},
                "summary": "",
                "experience": [
                    {
                        "company": "Acme",
                        "title": "Intern",
                        "start": "2026-06",
                        "end": "2026-08",
                        "location": "San Francisco, CA",
                        "bullets": ["Did the work"],
                    }
                ],
                "education": [
                    {
                        "school": "USC",
                        "degree": "BS",
                        "field": "Economics",
                        "start_year": "2023",
                        "end_year": "2027",
                        "details": ["Honors: Dean's List", "GPA: 3.9/4.0"],
                    }
                ],
                "skills": ["python"],
                "certifications": ["Series 79"],
                "languages": ["Hindi (native)"],
                "leadership": [
                    {
                        "company": "Value Investing Group",
                        "title": "Investment Associate",
                        "start": "2024-08",
                        "end": "present",
                        "location": "Los Angeles, CA",
                        "bullets": ["Admitted at ~1% acceptance rate"],
                    }
                ],
                "interests": ["Pickleball", "Cricket"],
            },
            today=self.TODAY,
        )
        assert data.startswith(b"%PDF-")
        from juicebox.docgen.parse import extract_pdf_text

        text = extract_pdf_text(data)
        assert "LEADERSHIP & INVOLVEMENT" in text
        assert "SKILLS & INTERESTS" in text
        assert "Series 79" in text
        assert "Dean's List" in text
        # Student record: education prints before the internship.
        assert text.index("EDUCATION") < text.index("PROFESSIONAL EXPERIENCE")


def _ink(pdf: bytes, page_number: int = 0) -> tuple[float, float]:
    """``(highest, lowest)`` text baseline on a page, in page points.

    ReportLab draws text inside a translated graphics state, so the text
    matrix alone is page-relative; composing it with the CTM is what gives a
    coordinate that can be compared against the paper.
    """
    import io

    from pypdf import PdfReader

    page = PdfReader(io.BytesIO(pdf)).pages[page_number]
    baselines: list[float] = []

    def visit(text, cm, tm, font_dict, font_size):
        if text.strip():
            baselines.append(cm[1] * tm[4] + cm[3] * tm[5] + cm[5])

    page.extract_text(visitor_text=visit)
    assert baselines, f"page {page_number + 1} drew no text"
    return max(baselines), min(baselines)


def _page_count(pdf: bytes) -> int:
    import io

    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(pdf)).pages)


class TestOnePageFit:
    """A resume is a one-page document, so the page setting flexes too.

    The complaint this answers, in a candidate's words: "it has nothing on
    here." A LinkedIn-only history — real jobs, real dates, no descriptions —
    was drawn at the same density as a fifteen-year career and left four
    inches of white below the last line, which reads as a broken export rather
    than a short career. The fix is typographic and only typographic: the same
    facts, set at whichever density on ``_LADDER`` lands inside the page
    budget.
    """

    TODAY = __import__("datetime").date(2026, 8, 25)

    def _career(self, roles: int, bullets: int) -> dict:
        return {
            "contact": {"name": "Dana Ruiz", "email": "dana@example.com"},
            "summary": "",
            "experience": [
                {
                    "company": f"Employer {n}",
                    "title": "Software Engineer",
                    "start": f"{2008 + n}-01",
                    "end": f"{2009 + n}-12",
                    "location": "New York, United States",
                    "bullets": [
                        f"Owned the {word} workstream from design through rollout."
                        for word in ("platform", "ingest", "billing", "search")[:bullets]
                    ],
                }
                for n in range(roles)
            ],
            "education": [{"school": "Rice University", "degree": "BS", "field": "CS"}],
            "skills": ["python", "react", "postgres"],
        }

    def _linkedin_only(self) -> dict:
        """Six real roles, no descriptions — the shape that prompted this."""
        career = self._career(roles=6, bullets=0)
        career["experience"][0]["bullets"] = ["Redefining the job application process"]
        return career

    def test_a_thin_history_is_set_to_fill_the_page(self):
        from juicebox.docgen.render import _REFERENCE, _draw, render_resume_pdf

        structured = self._linkedin_only()
        fitted = render_resume_pdf(structured, today=self.TODAY)
        fixed, _ = _draw(structured, self.TODAY, _REFERENCE, "x")

        # At one fixed density this resume stopped less than halfway down the
        # paper, leaving over four inches of white above the bottom margin.
        _, fixed_last_line = _ink(fixed)
        assert fixed_last_line - _REFERENCE.margin > 4 * 72

        # Fitted, the last line reaches the bottom quarter of the paper — most
        # of the white is gone, and what is left reads as a margin.
        _, fitted_last_line = _ink(fitted)
        assert fitted_last_line < 792 / 4
        assert fitted_last_line < fixed_last_line / 2
        assert _page_count(fitted) == 1

    def test_filling_the_page_adds_and_drops_nothing(self):
        from juicebox.docgen.parse import extract_pdf_text
        from juicebox.docgen.render import _REFERENCE, _draw, render_resume_pdf

        structured = self._linkedin_only()
        fitted = extract_pdf_text(render_resume_pdf(structured, today=self.TODAY))
        fixed = extract_pdf_text(_draw(structured, self.TODAY, _REFERENCE, "x")[0])
        # Whitespace is exactly what changed, so compare the words themselves.
        assert fitted.split() == fixed.split()

    def test_a_history_that_would_spill_is_squeezed_back_onto_one_page(self):
        from juicebox.docgen.render import _REFERENCE, _draw, render_resume_pdf

        structured = self._career(roles=8, bullets=4)
        assert _draw(structured, self.TODAY, _REFERENCE, "x")[1] == 2
        assert _page_count(render_resume_pdf(structured, today=self.TODAY)) == 1

    def test_a_career_too_long_for_one_page_keeps_every_job(self):
        from juicebox.docgen.parse import extract_pdf_text
        from juicebox.docgen.render import render_resume_pdf

        structured = self._career(roles=15, bullets=4)
        pdf = render_resume_pdf(structured, today=self.TODAY)
        assert _page_count(pdf) == 2
        text = extract_pdf_text(pdf)
        for job in structured["experience"]:
            assert job["company"] in text

    def test_a_second_page_is_a_spread_not_a_stub(self):
        """Two pages means the airiest two-page setting, not the tightest.

        Squeezing a fifteen-role career to 9pt when it needs two pages either
        way buys nothing and costs legibility; the room belongs to page two.
        """
        from juicebox.docgen.render import render_resume_pdf

        pdf = render_resume_pdf(self._career(roles=15, bullets=4), today=self.TODAY)
        top, bottom = _ink(pdf, page_number=1)
        assert top - bottom > 300
