"""Lever, via the postings apply endpoint -- with the employer's key.

``POST api.lever.co/v0/postings/{site}/{id}?key=APIKEY``. The key is generated
by a Super Admin of **that employer's** Lever account; the unauthenticated
``GET`` on the same path lists postings and is a different thing entirely. An
earlier version of this adapter sent an empty ``key=`` and claimed no
credential was needed -- it was wrong, and every submission it made would have
been refused.

Where we hold no key, the hosted form is the candidate's door and
``browser/hosted_form.py`` drives it.
"""

from __future__ import annotations

import logging

import httpx

from .base import METHOD_ATS_API, SubmissionContext, SubmissionResult
from .http import parts, post

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.lever.co/v0/postings/{site}/{posting_id}"

#: Lever stores the User-Agent on the application record. Identify
#: ourselves rather than leaking an HTTP library's default.
APPLICANT_USER_AGENT = "Foray/1.0 (+https://goforay.io; candidate application agent)"


class LeverSubmitter:
    vendor = "lever"
    method = METHOD_ATS_API

    def __init__(self, *, board_key: str = "", client: httpx.Client | None = None) -> None:
        self.board_key = board_key
        self._client = client

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        site = ctx.target.tenant
        posting_id = ctx.target.external_job_id
        if not site or not posting_id:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error=f"lever needs a site and a posting id (got {site!r}/{posting_id!r})",
            )
        if ctx.resume is None:
            return SubmissionResult(
                ok=False, method=self.method, needs_human=True, error="no resume in the package"
            )
        if not self.board_key:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error=f"no stored Lever key for site {site!r}",
            )

        fields = {
            "name": ctx.full_name,
            "email": ctx.from_address,
            "phone": ctx.phone,
            "urls[LinkedIn]": ctx.linkedin_url,
            "comments": ctx.answer("why_company"),
        }
        response, refusal = post(
            ENDPOINT.format(site=site, posting_id=posting_id),
            method=self.method,
            params={"key": self.board_key},
            data={key: value for key, value in fields.items() if value},
            files=parts(ctx),
            # Lever records these three on every application for anti-fraud.
            # Sending an httpx default would mark every candidate we represent
            # as the same suspicious client.
            headers={
                "User-Agent": APPLICANT_USER_AGENT,
                "Referer": ctx.target.apply_url or ctx.posting_url,
            },
            client=self._client,
        )
        if refusal is not None:
            return refusal
        assert response is not None
        return SubmissionResult(
            ok=True,
            method=self.method,
            external_id=_id_of(response),
            confirmation_ref=f"lever {site}, posting {posting_id}",
        )


def _id_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        value = data.get("id") or data.get("applicationId")
        if isinstance(value, (str, int)):
            return str(value)
    return ""
