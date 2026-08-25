"""Ashby, via the public application form API.

Ashby takes a JSON field-submission array rather than a flat form, and the
document upload is a separate multipart part on the same request. The job's
public id is the uuid in the posting URL.
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

    def __init__(self, *, client: httpx.Client | None = None) -> None:
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
            client=self._client,
        )
        if refusal is not None:
            return refusal
        assert response is not None
        return SubmissionResult(
            ok=True,
            method=self.method,
            external_id=_id_of(response),
            confirmation_ref=f"ashby {org}, posting {job_id}",
        )


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
