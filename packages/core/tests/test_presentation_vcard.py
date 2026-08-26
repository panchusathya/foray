"""vCard contact cards: structure, logo art, signed public route."""

from __future__ import annotations

import io

from PIL import Image
from sqlalchemy import select

from juicebox.messaging.jobcards import PURPOSE_JOBCARD, mint_jobcard_link
from juicebox.messaging.uploads import PURPOSE_UPLOAD
from juicebox.models import Candidate, JobPosting, JobProposal
from juicebox.presentation.logo import (
    contact_photo,
    fetch_logo,
    photo_for,
    posting_domain,
    sanitize_hostname,
)
from juicebox.presentation.vcard import build_vcard, playful_note, vcard_filename
from juicebox.repository import upsert_candidates
from juicebox.security import signed_intake_token


def _png(*, color=(40, 90, 180), size=32) -> bytes:
    image = Image.new("RGB", (size, size), color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _card(**over) -> dict:
    base = {
        "company": "Anthropic",
        "title": "ML Engineer",
        "location": "San Francisco (remote ok)",
        "reasons": ["Current title matches", "Python depth"],
        "url": "https://anthropic.com/careers/ml",
        "recruiter_only": {"score": 0.9},
    }
    base.update(over)
    return base


class TestVcard:
    def test_structure_and_fn(self):
        payload = build_vcard(_card(), photo_png=_png()).decode("utf-8")
        assert payload.startswith("BEGIN:VCARD\r\n")
        assert payload.endswith("END:VCARD\r\n")
        assert "FN:ML Engineer · Anthropic" in payload
        assert "ORG:Anthropic" in payload
        assert "TITLE:ML Engineer" in payload
        assert "URL:https://anthropic.com/careers/ml" in payload
        assert "PHOTO;ENCODING=b;TYPE=PNG:" in payload
        assert "VERSION:3.0" in payload
        assert "0.9" not in payload

    def test_escaping(self):
        payload = build_vcard(
            _card(company="Foo, Inc; Bar", title="Staff\\Engineer"),
            photo_png=b"",
        ).decode("utf-8")
        assert "ORG:Foo\\, Inc\\; Bar" in payload
        assert "TITLE:Staff\\\\Engineer" in payload

    def test_photo_line_is_folded_at_75_octets(self):
        payload = build_vcard(_card(), photo_png=_png(size=64))
        for line in payload.split(b"\r\n"):
            if line:
                assert len(line) <= 75, line[:80]

    def test_note_budget_and_reasons(self):
        note = playful_note(_card())
        assert "why you:" in note
        assert "Current title matches" in note
        assert "Python depth" in note
        assert "San Francisco" in note
        assert "tap back" in note
        assert len(note) <= 200
        assert "0.9" not in note
        long = playful_note(_card(reasons=["x" * 80, "y" * 80], location="z" * 80))
        assert len(long) <= 200
        assert "tap back" in long

    def test_filename_is_a_slug(self):
        assert vcard_filename(_card()) == "anthropic-ml-engineer.vcf"


class TestLogo:
    def test_monogram_is_deterministic(self):
        a = contact_photo(None, company="Anthropic")
        b = contact_photo(None, company="Anthropic")
        c = contact_photo(None, company="Figma")
        assert a == b
        assert a != c
        assert a[:8] == b"\x89PNG\r\n\x1a\n"

    def test_garbage_bytes_never_raise(self):
        out = contact_photo(b"not-an-image", company="Acme")
        assert out[:8] == b"\x89PNG\r\n\x1a\n"
        assert out == contact_photo(None, company="Acme")

    def test_fetch_logo_404_is_none(self):
        assert fetch_logo("anthropic.com", transport=lambda url: (404, b"")) is None
        assert fetch_logo("anthropic.com", transport=lambda url: (200, b"")) is None

    def test_fetch_logo_uses_google_s2(self):
        seen = []

        def transport(url):
            seen.append(url)
            return 200, _png()

        body = fetch_logo("anthropic.com", transport=transport)
        assert body == _png()
        assert seen and "google.com/s2/favicons" in seen[0]
        assert "domain=anthropic.com" in seen[0]
        assert "sz=256" in seen[0]

    def test_sanitize_rejects_ips_and_localhost(self):
        assert sanitize_hostname("127.0.0.1") == ""
        assert sanitize_hostname("localhost") == ""
        assert sanitize_hostname("evil.local") == ""
        assert sanitize_hostname("anthropic.com") == "anthropic.com"
        assert sanitize_hostname("https://www.anthropic.com/careers") == "anthropic.com"

    def test_posting_domain_skips_ats_hosts(self):
        posting = JobPosting(
            org_id="o",
            source="exa",
            source_id="x",
            company="Acme",
            source_url="https://boards.greenhouse.io/acme/jobs/1",
            apply_url="https://jobs.lever.co/acme/abcd",
        )
        assert posting_domain(posting) == ""
        posting.company_domain = "acme.com"
        assert posting_domain(posting) == "acme.com"

    def test_photo_for_with_injected_transport_skips_the_cache(self):
        png = photo_for("example.com", "Example", transport=lambda url: (200, _png()))
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


class TestJobcardRoute:
    def _proposal(self, session, account, sample_candidates, **posting_kw):
        upsert_candidates(
            session, org_id=account["org_id"], payloads=sample_candidates[:1]
        )
        session.flush()
        candidate = session.scalar(select(Candidate).where(Candidate.full_name == "Ada Okonkwo"))
        posting = JobPosting(
            org_id=account["org_id"],
            source="manual",
            source_id="card-route-1",
            title="ML Engineer",
            company="Anthropic",
            status="open",
            **posting_kw,
        )
        session.add(posting)
        session.flush()
        proposal = JobProposal(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
            status="proposed",
            reasons=["Current title matches"],
        )
        session.add(proposal)
        session.commit()
        return proposal

    def test_valid_token_returns_vcard(self, client, session, account, sample_candidates):
        proposal = self._proposal(session, account, sample_candidates)
        link = mint_jobcard_link(proposal)
        path = link.split("http://testserver", 1)[-1]
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/vcard")
        disp = response.headers.get("content-disposition", "")
        assert "filename=" in disp
        assert disp.endswith('.vcf"')
        body = response.content.decode("utf-8")
        assert "BEGIN:VCARD" in body
        assert "FN:ML Engineer · Anthropic" in body
        assert "Current title matches" in body
        assert "PHOTO;ENCODING=b;TYPE=PNG:" in body

    def test_wrong_token_is_404(self, client, session, account, sample_candidates):
        proposal = self._proposal(session, account, sample_candidates)
        response = client.get(f"/v1/intake/jobcards/{proposal.id}.vcf?token=forged")
        assert response.status_code == 404

    def test_unknown_proposal_is_404(self, client):
        token = signed_intake_token(PURPOSE_JOBCARD, "jprop_doesnotexist")
        response = client.get(f"/v1/intake/jobcards/jprop_doesnotexist.vcf?token={token}")
        assert response.status_code == 404

    def test_upload_purpose_token_cannot_be_replayed(
        self, client, session, account, sample_candidates
    ):
        proposal = self._proposal(session, account, sample_candidates)
        token = signed_intake_token(PURPOSE_UPLOAD, proposal.id)
        response = client.get(f"/v1/intake/jobcards/{proposal.id}.vcf?token={token}")
        assert response.status_code == 404
