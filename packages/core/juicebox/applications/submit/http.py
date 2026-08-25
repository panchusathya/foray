"""Shared HTTP posture for the API adapters.

Every ATS endpoint here is a public application form behind a different shape
of multipart POST. What they share is what happens when one of them is having a
bad day, and that is worth deciding once: a 4xx is *our* request being wrong
and will be wrong again, so it fails the attempt; a 5xx, a timeout or a
connection error is the vendor's and is worth the queue's backoff.
"""

from __future__ import annotations

import logging

import httpx

from .base import SubmissionResult, SubmitError

logger = logging.getLogger(__name__)

#: An application POST carries two PDFs. Slower than a page read, and the
#: retry is expensive, so give it room.
TIMEOUT_SECONDS = 45.0


def post(
    url: str,
    *,
    method: str,
    data: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    files: dict | None = None,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    client: httpx.Client | None = None,
) -> tuple[httpx.Response | None, SubmissionResult | None]:
    """``(response, None)`` on a 2xx, ``(None, result)`` on a decided failure.

    Raises ``SubmitError`` for anything the queue should retry rather than
    record as this application's answer.
    """
    owns = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True)
    try:
        try:
            response = client.post(
                url,
                data=data,
                params=params,
                files=files,
                json=json_body,
                headers=headers,
                auth=auth,
            )
        except httpx.HTTPError as exc:
            raise SubmitError(f"could not reach {url}: {exc}") from exc
        if response.status_code >= 500:
            raise SubmitError(f"{url} answered {response.status_code}")
        if response.status_code >= 400:
            # The body is the vendor telling us which field it disliked, and it
            # is the only thing that makes this diagnosable later.
            detail = response.text[:400].replace("\n", " ").strip()
            return None, SubmissionResult(
                ok=False,
                method=method,
                needs_human=True,
                error=f"HTTP {response.status_code}: {detail}",
            )
        return response, None
    finally:
        if owns:
            client.close()


def parts(ctx) -> dict:
    """Multipart file parts for whichever documents the package has."""
    built: dict = {}
    if ctx.resume is not None:
        built["resume"] = (ctx.resume.filename, ctx.resume.content, ctx.resume.content_type)
    if ctx.cover_letter is not None:
        built["cover_letter"] = (
            ctx.cover_letter.filename,
            ctx.cover_letter.content,
            ctx.cover_letter.content_type,
        )
    return built
