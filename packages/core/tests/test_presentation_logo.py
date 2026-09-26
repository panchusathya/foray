"""Employer logo art: the fetch, the monogram fallback, and the SSRF rules.

Split out of the vCard tests when the vCard went away. The art itself did not:
the contact card is gone but ``presentation/logo.py`` still fetches the
employer's mark, and ``messaging/cards.py`` now draws it into the job-card
image. These rules are the reason that fetch is safe to point at a hostname
taken off a posting -- Google's s2 endpoint is the only origin, and an ATS host
never becomes the employer.
"""

from __future__ import annotations

import io

from PIL import Image

from juicebox.models import JobPosting
from juicebox.presentation.logo import (
    contact_photo,
    fetch_logo,
    posting_domain,
    sanitize_hostname,
)


def _png(*, color=(40, 90, 180), size=32) -> bytes:
    image = Image.new("RGB", (size, size), color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class TestLogo:
    def test_garbage_bytes_never_raise(self):
        out = contact_photo(b"not-an-image", company="Acme")
        assert out[:8] == b"\x89PNG\r\n\x1a\n"
        assert out == contact_photo(None, company="Acme")

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
