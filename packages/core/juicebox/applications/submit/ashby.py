"""Ashby, via ``applicationForm.submit`` -- with the employer's key.

Basic auth, the employer's API key as the username with an empty password, and
the key needs ``candidatesWrite``. Ashby takes a JSON field-submission array
rather than a flat form, and the document rides as a multipart part on the same
request.

**Ashby answers 200 with ``success: false`` when it rejects an application**,
and in that case the application is not recorded. Trusting the status code
here would mean telling a candidate we applied when we had not, which is the
worst failure available to this module -- so the body is checked and its
validation errors are what the attempt row records.

Where we hold no key, the hosted form is the candidate's door and the
browser agent drives it when configured.
"""

from __future__ import annotations

import json
import logging

import httpx

from .base import METHOD_ATS_API, SubmissionContext, SubmissionResult
from .http import parts, post

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.ashbyhq.com/posting-api/job-board/{org}/application-form"


class AshbySubmitter:
    vendor = "ashby"
    method = METHOD_ATS_API

    def __init__(self, *, board_key: str = "", client: httpx.Client | None = None) -> None:
        self.board_key = board_key
        self._client = client

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        org = ctx.target.tenant
        job_id = ctx.target.external_job_id
        if not org or not job_id:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error=f"ashby needs an org and a job id (got {org!r}/{job_id!r})",
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
                error=f"no stored Ashby key for org {org!r}",
            )

        field_submissions = [
            {"path": "_systemfield_name", "value": ctx.full_name},
            {"path": "_systemfield_email", "value": ctx.from_address},
        ]
        if ctx.phone:
            field_submissions.append({"path": "_systemfield_phone", "value": ctx.phone})
        if ctx.resume is not None:
            field_submissions.append({"path": "_systemfield_resume", "value": "resume"})

        response, refusal = post(
            ENDPOINT.format(org=org),
            method=self.method,
            data={
                "jobPostingId": job_id,
                "fieldSubmissions": json.dumps(field_submissions),
            },
            files=parts(ctx),
            auth=(self.board_key, ""),
            headers={"Accept": "application/json; version=1"},
            client=self._client,
        )
        if refusal is not None:
            return refusal
        assert response is not None

        # A 200 is not an acceptance. Ashby reports validation failures in the
        # body, and an application it did not record must not read as sent.
        accepted, detail = _accepted(response)
        if not accepted:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error=f"ashby rejected the application: {detail}",
            )
        return SubmissionResult(
            ok=True,
            method=self.method,
            external_id=_id_of(response),
            confirmation_ref=f"ashby {org}, posting {job_id}",
        )


def _accepted(response: httpx.Response) -> tuple[bool, str]:
    """``(accepted, detail)`` from Ashby's own verdict, not from the status.

    An unparseable body is treated as a refusal: we would rather re-check by
    hand than record a submission we cannot evidence.
    """
    try:
        body = response.json()
    except ValueError:
        return False, "unreadable response body"
    if not isinstance(body, dict):
        return False, "unexpected response shape"
    if body.get("success") is True:
        return True, ""
    errors = body.get("errors") or body.get("error") or body
    return False, str(errors)[:300]


def _id_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        results = body.get("results")
        if isinstance(results, dict):
            value = results.get("id") or results.get("applicationId")
            if isinstance(value, (str, int)):
                return str(value)
    return ""
