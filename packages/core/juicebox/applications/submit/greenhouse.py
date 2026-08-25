"""Greenhouse, via the Job Board API -- only where the employer gave us a key.

``POST boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}`` authenticates with
a Job Board API key generated inside **that employer's** Greenhouse account.
There is no key that spans boards. For any posting where we hold none -- which
is the whole open market -- the candidate's door is the hosted form and
``browser/hosted_form.py`` is what drives it. This adapter is only reached when
``credentials.board_key`` returned something.

Two ids matter and they are not interchangeable: the board token (the employer's
namespace, ``boards.greenhouse.io/<token>``) and the numeric job id. Both come
out of ``ats.detect``, so an adapter that cannot find one refuses rather than
guessing at a URL.

Greenhouse boards vary in which questions are required, and the required set is
per-posting. We send the standard set and let the endpoint reject what it does
not like -- a 4xx here is a form we do not understand, which is a
``needs_human``, not a retry.
"""

from __future__ import annotations

import logging

import httpx

from .base import METHOD_ATS_API, SubmissionContext, SubmissionResult
from .http import parts, post

logger = logging.getLogger(__name__)

ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"


class GreenhouseSubmitter:
    vendor = "greenhouse"
    method = METHOD_ATS_API

    def __init__(self, *, board_key: str = "", client: httpx.Client | None = None) -> None:
        self.board_key = board_key
        self._client = client

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        board = ctx.target.tenant
        job_id = ctx.target.external_job_id
        if not board or not job_id:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error=f"greenhouse needs a board token and a job id (got {board!r}/{job_id!r})",
            )
        if ctx.resume is None:
            return SubmissionResult(
                ok=False, method=self.method, needs_human=True, error="no resume in the package"
            )

        if not self.board_key:
            # The registry should never route here without one; if it did, say
            # so plainly rather than posting unauthenticated and getting a 401.
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error=f"no stored Greenhouse board key for {board!r}",
            )

        fields = {
            "first_name": ctx.first_name,
            "last_name": ctx.last_name,
            "email": ctx.from_address,
        }
        if ctx.phone:
            fields["phone"] = ctx.phone
        if ctx.linkedin_url:
            fields["linkedin_profile"] = ctx.linkedin_url
        # Greenhouse asks for the letter as a file when the posting has the
        # field, and ignores it otherwise.
        response, refusal = post(
            ENDPOINT.format(board=board, job_id=job_id),
            method=self.method,
            data={key_: value for key_, value in fields.items() if value},
            files=parts(ctx),
            # Basic auth, key as the username with an empty password.
            auth=(self.board_key, ""),
            client=self._client,
        )
        if refusal is not None:
            return refusal
        assert response is not None
        return SubmissionResult(
            ok=True,
            method=self.method,
            external_id=_application_id(response),
            confirmation_ref=f"greenhouse board {board}, job {job_id}",
        )


def _application_id(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        for key in ("id", "application_id", "success"):
            value = body.get(key)
            if isinstance(value, (str, int)):
                return str(value)
    return ""
