"""A resume is a first-class answer, whatever channel and format it arrives in.

The promise under test: "send us your resume" works the way "send us your
LinkedIn" does. That means a DOCX is as good as a PDF (magic-byte sniffed,
never trusted by header), an iMessage attachment is as good as the upload
page, a resume that lands before the thread has an identity waits for one
instead of vanishing, and a file we cannot read gets an honest reply that
keeps the conversation moving.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy import select

from juicebox import storage
from juicebox.docgen.parse import (
    DOCX_MIME,
    PDF_MIME,
    extract_docx_text,
    extract_text,
    sniff_resume_type,
)
from juicebox.docgen.render import render_resume_pdf
from juicebox.enrich.promote import payload_from_resume_v1, promote
from juicebox.jobs import (
    KIND_DOC_PARSE,
    KIND_INTAKE_ATTACHMENT,
    KIND_INTAKE_TURN,
    enqueue,
)
from juicebox.llm.client import FakeTransport, LLMClient, set_llm
from juicebox.messaging.attachments import (
    PENDING_KEY,
    ingest_inbound_attachments,
    materialize_pending,
)
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.conversation import run_turn
from juicebox.messaging.linkedin_flow import (
    FLOW_LINKEDIN_SHORT,
    flow_meta,
    profile_from_resume,
)
from juicebox.messaging.linq import (
    InboundAttachment,
    MediaFetchError,
    fetch_media,
    parse_inbound,
)
from juicebox.messaging.threads import open_thread, record_inbound
from juicebox.models import Candidate, CandidateDocument, Job
from juicebox.worker import process_job

RESUME_V1 = {
    "contact": {
        "name": "Ada Lovelace",
        "email": "ada@lovelace.dev",
        "phone": "",
        "location": "San Francisco, CA",
        "linkedin_url": "https://linkedin.com/in/ada",
        "github_url": "https://github.com/ada",
        "website_url": "https://ada.dev",
    },
    "summary": "Strategic finance analyst with a banking background.",
    "experience": [
        {
            "company": "Westbridge Capital",
            "title": "Investor",
            "start": "2024-01",
            "end": "present",
            "bullets": ["Diligence and financial modelling on growth-stage software."],
        }
    ],
    "education": [
        {
            "school": "Berkeley",
            "degree": "BA",
            "field": "Economics",
            "start_year": "2016",
            "end_year": "2020",
        }
    ],
    "skills": ["financial modeling", "valuation"],
    "certifications": [],
    "languages": ["Spanish (fluent)"],
}

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_bytes(paragraphs: list[str], header: str = "") -> bytes:
    """A minimal real DOCX — a zip with the parts our extractor reads."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>',
        )
        if header:
            archive.writestr(
                "word/header1.xml",
                f'<w:hdr xmlns:w="{_W}"><w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>',
            )
    return buffer.getvalue()


def _pdf_bytes(summary: str = "Investor at Westbridge Capital since 2024.") -> bytes:
    """A real PDF made by our own renderer — no binary fixtures in the repo."""
    return render_resume_pdf({**RESUME_V1, "summary": summary})


@pytest.fixture
def channel():
    fake = FakeChannel(consent=True)
    set_channel(LINQ, fake)
    return fake


@pytest.fixture
def thread(session, account, channel):
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-resume",
        entry_point="linq_inbound",
        from_address="+14155550123",
        consent_messaging=True,
        flow=FLOW_LINKEDIN_SHORT,
    )


def _say(session, thread, body, native_id):
    record_inbound(session, thread=thread, body=body, provider_native_id=native_id)
    return run_turn(session, thread=thread)


def _pdf_attachment(url: str = "https://cdn.linqapp.com/att-1") -> InboundAttachment:
    return InboundAttachment(url=url, filename="ada.pdf", content_type="application/pdf")


class TestSniffAndExtract:
    def test_magic_bytes_decide_not_the_header(self):
        assert sniff_resume_type(_pdf_bytes()) == PDF_MIME
        assert sniff_resume_type(_docx_bytes(["Ada"])) == DOCX_MIME
        assert sniff_resume_type(b"\x89PNG\r\n\x1a\n....") == ""
        assert sniff_resume_type(b"") == ""

    def test_a_zip_without_a_word_body_is_not_a_docx(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
        assert sniff_resume_type(buffer.getvalue()) == ""

    def test_docx_text_keeps_the_body_and_the_header(self):
        data = _docx_bytes(
            ["Investor at Westbridge Capital", "Berkeley, BA Economics"],
            header="Ada Lovelace — ada@lovelace.dev",
        )
        text = extract_docx_text(data)
        assert "Investor at Westbridge Capital" in text
        assert "Berkeley" in text
        # Resumes routinely put the name and contact line in a header;
        # dropping it drops the person.
        assert "ada@lovelace.dev" in text

    def test_extract_text_dispatches_on_content(self):
        assert "Westbridge" in extract_text(_docx_bytes(["Investor at Westbridge"]))
        assert "Westbridge" in extract_text(_pdf_bytes())
        assert extract_text(b"not a resume at all") == ""

    def test_a_truncated_docx_is_empty_not_a_crash(self):
        assert extract_docx_text(_docx_bytes(["Ada"])[:40]) == ""


class TestUploadAcceptsDocx:
    @pytest.fixture
    def candidate(self, session, account):
        from juicebox.repository import upsert_candidates

        upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[{"source": "test", "source_id": "ada", "full_name": "Ada Lovelace"}],
        )
        session.commit()
        return session.query(Candidate).filter_by(full_name="Ada Lovelace").one()

    def test_a_docx_upload_lands_with_its_own_type(self, client, session, account, candidate):
        response = client.post(
            f"/v1/candidates/{candidate.id}/documents",
            files={"file": ("ada.docx", _docx_bytes(["Investor at Westbridge"]), DOCX_MIME)},
            headers=account["headers"],
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["content_type"] == DOCX_MIME
        doc = session.get(CandidateDocument, body["id"])
        assert doc.storage_path.endswith(".docx")
        assert session.scalar(select(Job).where(Job.kind == KIND_DOC_PARSE)) is not None

    def test_a_lying_content_type_header_does_not_help(self, client, account, candidate):
        response = client.post(
            f"/v1/candidates/{candidate.id}/documents",
            files={"file": ("resume.pdf", b"\x89PNG\r\n\x1a\n....", "application/pdf")},
            headers=account["headers"],
        )
        assert response.status_code == 415

    def test_the_upload_page_takes_a_docx_too(self, session, account, thread, channel, client):
        from juicebox.messaging.uploads import mint_upload_link

        _say(session, thread, "hey", "d0")
        _say(session, thread, "ada@lovelace.dev", "d1")
        session.commit()
        link = mint_upload_link(thread)
        response = client.post(
            link[link.index("/v1/") :],
            files={"file": ("ada.docx", _docx_bytes(["Investor at Westbridge"]), DOCX_MIME)},
        )
        assert response.status_code == 201, response.text
        doc = session.scalar(
            select(CandidateDocument).where(CandidateDocument.thread_id == thread.id)
        )
        assert doc is not None
        assert doc.content_type == DOCX_MIME


class TestInboundAttachmentParsing:
    def test_media_parts_become_attachments(self):
        payload = {
            "data": {
                "chat_id": "chat-1",
                "sender_handle": "+14155550123",
                "id": "msg-1",
                "parts": [
                    {"type": "text", "value": "here's my resume"},
                    {
                        "type": "media",
                        "url": "https://cdn.linqapp.com/f/abc",
                        "content_type": "application/pdf",
                        "filename": "ada.pdf",
                        "size": 12345,
                        "attachment_id": "att-1",
                    },
                ],
            }
        }
        inbound = parse_inbound(payload)
        assert inbound.body == "here's my resume"
        assert len(inbound.attachments) == 1
        attachment = inbound.attachments[0]
        assert attachment.url == "https://cdn.linqapp.com/f/abc"
        assert attachment.filename == "ada.pdf"
        assert attachment.attachment_id == "att-1"
        # Round-trips through the job payload without loss.
        assert InboundAttachment.from_dict(attachment.to_dict()) == attachment

    def test_a_media_part_without_a_url_is_dropped(self):
        payload = {
            "data": {
                "chat_id": "chat-1",
                "sender_handle": "+14155550123",
                "id": "msg-1",
                "parts": [{"type": "media"}],
            }
        }
        assert parse_inbound(payload).attachments == ()


class TestFetchMedia:
    def test_happy_path_returns_the_bytes(self):
        data = fetch_media("https://cdn/x", transport=lambda url: (200, b"%PDF-1.4"))
        assert data == b"%PDF-1.4"

    def test_a_dead_url_raises(self):
        with pytest.raises(MediaFetchError):
            fetch_media("https://cdn/x", transport=lambda url: (403, b""))

    def test_the_upload_cap_applies_to_downloads_too(self):
        from juicebox.config import get_settings

        oversized = b"x" * (get_settings().max_upload_bytes + 1)
        with pytest.raises(MediaFetchError):
            fetch_media("https://cdn/x", transport=lambda url: (200, oversized))


class TestAttachmentIngest:
    def test_a_resume_to_a_known_sender_stores_and_queues_the_parse(
        self, session, account, thread, channel
    ):
        _say(session, thread, "hey", "a0")
        _say(session, thread, "ada@lovelace.dev", "a1")

        result = ingest_inbound_attachments(
            session,
            thread=thread,
            message_id="msg-a2",
            attachments=[_pdf_attachment()],
            had_text=False,
            media_transport=lambda url: (200, _pdf_bytes()),
        )
        assert "stored" in result
        doc = session.scalar(
            select(CandidateDocument).where(CandidateDocument.thread_id == thread.id)
        )
        assert doc is not None and doc.is_default
        assert session.scalar(select(Job).where(Job.kind == KIND_DOC_PARSE)) is not None

    def test_a_resume_before_any_identity_waits_for_one(
        self, session, account, thread, channel
    ):
        """First message ever is the file. The bytes are taken immediately —
        the signed URL may be minutes from expiry — and attached the moment
        the email arrives."""
        record_inbound(session, thread=thread, body="", provider_native_id="p0")
        result = ingest_inbound_attachments(
            session,
            thread=thread,
            message_id="p0",
            attachments=[_pdf_attachment()],
            had_text=False,
            media_transport=lambda url: (200, _pdf_bytes()),
        )
        assert "pending" in result
        pending = flow_meta(thread).get(PENDING_KEY)
        assert pending and storage.read_bytes(pending["relpath"])
        # No document yet — nobody to own it.
        assert (
            session.scalar(
                select(CandidateDocument).where(CandidateDocument.thread_id == thread.id)
            )
            is None
        )
        # The conversation was woken so the intro can ask for the email.
        assert session.scalar(select(Job).where(Job.kind == KIND_INTAKE_TURN)) is not None

        run_turn(session, thread=thread)  # intro: asks for the email
        _say(session, thread, "ada@lovelace.dev", "p1")

        doc = session.scalar(
            select(CandidateDocument).where(CandidateDocument.thread_id == thread.id)
        )
        assert doc is not None, "the held resume attaches once the email lands"
        assert not flow_meta(thread).get(PENDING_KEY), "the stash is spent"
        assert session.scalar(select(Job).where(Job.kind == KIND_DOC_PARSE)) is not None

    def test_materialize_is_a_noop_without_a_stash(self, session, thread):
        assert materialize_pending(session, thread=thread) is None

    def test_a_photo_gets_an_honest_reply_with_the_fallback_link(
        self, session, account, thread, channel
    ):
        _say(session, thread, "hey", "n0")
        _say(session, thread, "ada@lovelace.dev", "n1")
        result = ingest_inbound_attachments(
            session,
            thread=thread,
            message_id="msg-n2",
            attachments=[InboundAttachment(url="https://cdn/pic", filename="pic.jpg")],
            had_text=False,
            media_transport=lambda url: (200, b"\x89PNG\r\n\x1a\n...."),
        )
        assert result["replied"] == "unreadable_attachment"
        body = channel.bodies[-1]
        assert "can't read" in body.lower()
        assert "/v1/intake/uploads/" in body

    def test_a_failed_download_falls_back_to_the_upload_page(
        self, session, account, thread, channel
    ):
        _say(session, thread, "hey", "f0")
        _say(session, thread, "ada@lovelace.dev", "f1")

        def dead(url):
            raise MediaFetchError("expired")

        result = ingest_inbound_attachments(
            session,
            thread=thread,
            message_id="msg-f2",
            attachments=[_pdf_attachment()],
            had_text=False,
            media_transport=dead,
        )
        assert result["download_failed"] is True
        body = channel.bodies[-1]
        assert "didn't come through" in body.lower()
        assert "/v1/intake/uploads/" in body

    def test_text_with_a_junk_file_still_gets_its_turn(self, session, account, thread, channel):
        """"here's my resume" plus a photo: the words still deserve an answer,
        but through the ordinary turn, not the unreadable-file copy — the
        thread already has a profile answer here, so the file was not it."""
        _say(session, thread, "hey", "t0")
        _say(session, thread, "ada@lovelace.dev", "t1")
        thread.contact_linkedin = "https://linkedin.com/in/ada"
        session.flush()
        record_inbound(session, thread=thread, body="what roles?", provider_native_id="t2")
        result = ingest_inbound_attachments(
            session,
            thread=thread,
            message_id="msg-t2",
            attachments=[InboundAttachment(url="https://cdn/pic")],
            had_text=True,
            media_transport=lambda url: (200, b"\x89PNG junk"),
        )
        assert result == {"turn": True}
        turn = session.scalar(
            select(Job).where(Job.kind == KIND_INTAKE_TURN).order_by(Job.created_at.desc())
        )
        assert turn is not None


class TestEndToEndOverTheWebhook:
    def test_an_imessage_resume_builds_the_profile_like_linkedin_does(
        self, session, account, thread, channel, monkeypatch
    ):
        """The flagship path: attach a resume in the thread, end up with a
        promoted candidate and the same confirm message a scrape earns."""
        _say(session, thread, "hey", "e0")
        _say(session, thread, "ada@lovelace.dev", "e1")

        monkeypatch.setattr(
            "juicebox.messaging.attachments.fetch_media",
            lambda url, **kwargs: _pdf_bytes(),
        )
        record_inbound(session, thread=thread, body="", provider_native_id="e2")
        enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_INTAKE_ATTACHMENT,
            idempotency_key=f"{KIND_INTAKE_ATTACHMENT}:e2",
            payload={
                "thread_id": thread.id,
                "message_id": "e2",
                "attachments": [_pdf_attachment().to_dict()],
                "had_text": False,
            },
        )
        set_llm(LLMClient(transport=FakeTransport(default=RESUME_V1), enabled=True))
        try:
            for _ in range(6):
                job = session.scalar(select(Job).where(Job.status == "queued"))
                if job is None:
                    break
                process_job(session, job)
                session.flush()
        finally:
            set_llm(None)

        candidate = session.get(Candidate, thread.candidate_id)
        assert candidate is not None
        companies = {exp.company for exp in candidate.experiences}
        assert "Westbridge Capital" in companies
        assert candidate.github_url == "https://github.com/ada"
        assert candidate.website_url == "https://ada.dev"

        doc = session.scalar(
            select(CandidateDocument).where(CandidateDocument.thread_id == thread.id)
        )
        assert doc.structured["languages"] == ["Spanish (fluent)"]

        confirm = channel.bodies[-1].lower()
        assert "westbridge capital" in confirm

    def test_an_unreadable_resume_falls_back_to_questions(
        self, session, account, thread, channel
    ):
        """No model configured: the parse honestly fails, and the thread says
        so and asks the gap-fill questions instead of holding forever."""
        _say(session, thread, "hey", "g0")
        _say(session, thread, "ada@lovelace.dev", "g1")

        ingest_inbound_attachments(
            session,
            thread=thread,
            message_id="msg-g2",
            attachments=[_pdf_attachment()],
            had_text=False,
            media_transport=lambda url: (200, _pdf_bytes()),
        )
        for _ in range(6):
            job = session.scalar(select(Job).where(Job.status == "queued"))
            if job is None:
                break
            process_job(session, job)
            session.flush()

        assert flow_meta(thread).get("error"), "the failure is on the thread"
        body = channel.bodies[-1].lower()
        assert "couldn't read that resume" in body


class TestSchemaBreadth:
    def test_promotion_carries_the_links(self, session, account):
        payload = payload_from_resume_v1(RESUME_V1)
        assert payload["github_url"] == "https://github.com/ada"
        assert payload["website_url"] == "https://ada.dev"
        result = promote(session, org_id=account["org_id"], parsed=payload)
        candidate = session.get(Candidate, result.candidate_id)
        assert candidate.github_url == "https://github.com/ada"
        assert candidate.linkedin_url == "https://linkedin.com/in/ada"

    def test_profile_from_resume_reads_the_contact_block(self, session, account):
        candidate = Candidate(org_id=account["org_id"], full_name="", source="t", source_id="t")
        profile = profile_from_resume(candidate, RESUME_V1)
        assert profile.url == "https://linkedin.com/in/ada"
        assert profile.full_name == "Ada Lovelace"
        assert profile.current_title == "Investor"
        assert profile.current_employer == "Westbridge Capital"

    def test_the_renderer_takes_the_new_fields_in_stride(self):
        data = render_resume_pdf(RESUME_V1)
        assert data.startswith(b"%PDF-")
