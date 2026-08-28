"""Candidate documents: upload discipline, parse honesty, docgen guards.

Two of these tests are the domain's conscience:

* the tailor-honesty test scripts a transport that *tries* to invent an
  employer and asserts the guard throws the output away, and
* the empty-text test proves a scanned PDF becomes a flagged row, never a
  crash and never a fabricated history.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from juicebox import storage
from juicebox.docgen import parse as parse_module
from juicebox.docgen.parse import ERROR_NO_TEXT, extract_pdf_text, parse_resume_text
from juicebox.docgen.render import render_resume_pdf
from juicebox.docgen.resume import build_resume_json
from juicebox.jobs import KIND_DOC_PARSE
from juicebox.llm.client import FakeTransport, LLMClient, set_llm
from juicebox.models import Candidate, CandidateDocument, Job
from juicebox.repository import upsert_candidates
from juicebox.worker import process_batch


@pytest.fixture
def candidate(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    return session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()


def _pdf_bytes(text: str = "Ada Okonkwo. Senior ML Engineer at Modal since 2019.") -> bytes:
    """A real PDF made by our own renderer -- no binary fixtures in the repo."""
    return render_resume_pdf(
        {
            "contact": {"name": "Ada Okonkwo", "email": "", "phone": "", "location": "",
                        "linkedin_url": ""},
            "summary": text,
            "experience": [],
            "education": [],
            "skills": [],
            "certifications": [],
        }
    )


def _upload(client, account, candidate, data: bytes, *, filename="resume.pdf", kind=""):
    params = {"kind": kind} if kind else {}
    return client.post(
        f"/v1/candidates/{candidate.id}/documents",
        params=params,
        files={"file": (filename, data, "application/pdf")},
        headers=account["headers"],
    )


class TestUpload:
    def test_upload_parses_and_defaults(self, client, session, account, candidate):
        session.commit()
        response = _upload(client, account, candidate, _pdf_bytes())
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "resume_uploaded"
        assert body["is_default"] is True
        assert body["sha256"]

        # The parse job was queued with the document as its idempotency key.
        job = session.scalar(select(Job).where(Job.kind == KIND_DOC_PARSE))
        assert job is not None and job.payload["document_id"] == body["id"]

        # A second upload takes over the default; exactly one default remains.
        second = _upload(client, account, candidate, _pdf_bytes("Newer resume."))
        assert second.json()["is_default"] is True
        defaults = session.scalars(
            select(CandidateDocument).where(
                CandidateDocument.candidate_id == candidate.id,
                CandidateDocument.is_default.is_(True),
            )
        ).all()
        session.expire_all()
        defaults = session.scalars(
            select(CandidateDocument).where(
                CandidateDocument.candidate_id == candidate.id,
                CandidateDocument.is_default.is_(True),
            )
        ).all()
        assert len(defaults) == 1 and defaults[0].id == second.json()["id"]

    def test_not_a_pdf_is_415(self, client, session, account, candidate):
        session.commit()
        # The header claims PDF; the bytes say otherwise. Bytes win.
        response = _upload(client, account, candidate, b"<html>hi</html>")
        assert response.status_code == 415

    def test_oversize_is_413(self, client, session, account, candidate, monkeypatch):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("JUICEBOX_MAX_UPLOAD_BYTES", str(1024))
        reset_settings_cache()
        session.commit()
        big = b"%PDF-" + b"x" * 4096
        response = _upload(client, account, candidate, big)
        assert response.status_code == 413
        reset_settings_cache()

    def test_download_and_delete(self, client, session, account, candidate):
        session.commit()
        uploaded = _upload(client, account, candidate, _pdf_bytes()).json()
        response = client.get(
            f"/v1/candidates/{candidate.id}/documents/{uploaded['id']}/download",
            headers=account["headers"],
        )
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF-")
        assert "attachment" in response.headers["content-disposition"]

        doc = session.get(CandidateDocument, uploaded["id"])
        relpath = doc.storage_path
        assert storage.document_abspath(relpath).exists()
        deleted = client.delete(
            f"/v1/candidates/{candidate.id}/documents/{uploaded['id']}",
            headers=account["headers"],
        )
        assert deleted.status_code == 204
        assert not storage.document_abspath(relpath).exists()

    def test_cross_candidate_is_a_404(self, client, session, account, candidate):
        session.commit()
        uploaded = _upload(client, account, candidate, _pdf_bytes()).json()
        response = client.get(
            f"/v1/candidates/cand_someoneelse/documents/{uploaded['id']}/download",
            headers=account["headers"],
        )
        assert response.status_code == 404


class TestParse:
    def test_worker_extracts_text_offline(self, client, session, account, candidate):
        """With no model, the text is kept and the row says exactly why the
        structured half is empty -- re-runnable when a key arrives."""
        session.commit()
        uploaded = _upload(client, account, candidate, _pdf_bytes("A distinctive summary line."))
        assert uploaded.status_code == 201
        processed = process_batch(session, worker="test")
        assert processed >= 1

        doc = session.get(CandidateDocument, uploaded.json()["id"])
        assert "distinctive summary line" in doc.text_extracted
        assert doc.structured == {}
        assert doc.error.startswith("llm_unavailable")
        assert doc.status == "ready"

    def test_garbage_bytes_flag_no_text(self):
        assert extract_pdf_text(b"not a pdf at all") == ""
        structured, error = parse_resume_text("")
        assert structured == {} and error == ERROR_NO_TEXT

    def test_resume_parse_uses_bounded_fast_model(self, monkeypatch):
        class Client:
            available = True

            def __init__(self):
                self.kwargs = None

            def json_fast(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(
                    data={
                        "contact": {"name": "Ada Okonkwo"},
                        "experience": [{"company": "Modal", "title": "Engineer"}],
                    }
                )

        client = Client()
        monkeypatch.setattr(parse_module, "get_llm", lambda: client)

        structured, error = parse_resume_text("Ada Okonkwo, Engineer at Modal")

        assert error == ""
        assert structured["contact"]["name"] == "Ada Okonkwo"
        assert client.kwargs["timeout"] == 30.0
        assert client.kwargs["max_tokens"] == 8000


class TestTailorHonesty:
    def test_default_upload_supersedes_candidate_and_linkedin_facts(
        self, session, account, candidate
    ):
        authored = {
            "contact": {"name": "Ada O.", "email": "authored@example.com"},
            "summary": "Candidate-authored summary.",
            "experience": [
                {
                    "company": "Candidate Authored Co",
                    "title": "Engineer",
                    "start": "2022",
                    "end": "present",
                    "location": "",
                    "bullets": ["Built the product named in my resume."],
                }
            ],
            "education": [],
            "skills": ["Candidate-authored skill"],
            "certifications": [],
        }
        session.add(
            CandidateDocument(
                org_id=account["org_id"],
                candidate_id=candidate.id,
                kind="resume_uploaded",
                is_default=True,
                status="ready",
                source="upload",
                structured=authored,
            )
        )
        session.flush()

        resume, generated_by = build_resume_json(session, candidate)

        assert generated_by == "upload"
        assert resume["summary"] == "Candidate-authored summary."
        assert [row["company"] for row in resume["experience"]] == ["Candidate Authored Co"]
        assert resume["skills"] == ["Candidate-authored skill"]
        assert resume["leadership"] == []  # schema shape only; no LinkedIn graft

    def test_an_inventing_model_is_thrown_away(self, session, account, candidate):
        """The adversarial case: the transport returns a plausible resume with
        an employer the candidate never had. The guard must ship the
        deterministic resume instead."""
        dishonest = {
            "contact": {"name": "Ada Okonkwo", "email": "ada@example.com", "phone": "",
                        "location": "", "linkedin_url": ""},
            "summary": "Veteran engineer.",
            "experience": [
                {"company": "Modal", "title": "Senior ML Engineer", "start": "2019-01",
                 "end": "present", "bullets": []},
                # The invention:
                {"company": "Google DeepMind", "title": "Staff Engineer", "start": "2014-01",
                 "end": "2016-01", "bullets": ["Led Gemini"]},
            ],
            "education": [],
            "skills": ["Python"],
            "certifications": [],
        }
        set_llm(LLMClient(transport=FakeTransport(default=dishonest), enabled=True))
        try:
            resume, generated_by = build_resume_json(session, candidate)
        finally:
            set_llm(None)
        assert generated_by == "template"
        companies = {job["company"] for job in resume["experience"]}
        assert "Google DeepMind" not in companies

    def test_an_honest_model_is_kept(self, session, account, candidate):
        honest = {
            "contact": {"name": "Ada Okonkwo", "email": "ada@example.com", "phone": "",
                        "location": "", "linkedin_url": ""},
            "summary": "ML engineer focused on LLM inference platforms.",
            "experience": [
                {"company": "Modal", "title": "Senior Machine Learning Engineer",
                 "start": "2019-01", "end": "present",
                 "bullets": ["Owned the production LLM inference platform."]},
            ],
            "education": [],
            "skills": ["Python", "PyTorch"],
            "certifications": [],
        }
        set_llm(LLMClient(transport=FakeTransport(default=honest), enabled=True))
        try:
            resume, generated_by = build_resume_json(session, candidate)
        finally:
            set_llm(None)
        assert generated_by == "llm"
        assert resume["summary"].startswith("ML engineer")

    def test_deterministic_resume_is_complete(self, session, candidate):
        resume, generated_by = build_resume_json(session, candidate)
        assert generated_by == "template"
        assert resume["contact"]["name"] == "Ada Okonkwo"
        assert resume["contact"]["email"] == "ada@example.com"
        companies = [job["company"] for job in resume["experience"]]
        assert companies == ["Modal", "Stripe"]  # current first
        assert resume["experience"][0]["end"] == "present"
        assert resume["education"][0]["school"] == "Stanford University"
        # Skills arrive normalized by the enrich pipeline.
        assert "python" in resume["skills"]
