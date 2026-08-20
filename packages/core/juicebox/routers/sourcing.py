"""Sourcing actions that cost money, so they need a name attached.

Search and enrichment go straight from the browser to Foray through the nginx
``/foray/`` proxy, which is unauthenticated and free. Buying contact details is
neither, so it comes through here instead: an org-scoped principal with write
permission, a required reason, and a hard cap on how many candidates one click
can pay for.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..deps import Principal, require_write
from ..integrations.foray import ForayError, get_foray

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sourcing", tags=["sourcing"])

# Foray enforces the same ceiling on its own side; this one exists so an
# oversized request is refused before it reaches a provider at all.
MAX_PER_CALL = 100


class FindEmailsRequest(BaseModel):
    logins: list[str] = Field(min_length=1, max_length=MAX_PER_CALL)
    # Free text, stored on Foray's shortlist_events. It is what makes a spend
    # attributable afterwards, which is why it is required rather than optional.
    reason: str = Field(min_length=3, max_length=200)


@router.post("/find-emails")
async def find_emails(
    payload: FindEmailsRequest,
    principal: Principal = Depends(require_write),
) -> dict:
    forays = get_foray()
    if not forays.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email enrichment is not configured on this deployment",
        )
    try:
        result = await forays.find_emails(payload.logins, payload.reason)
    except ForayError as exc:
        # 502, not 500: the failure is in a service behind us, and the caller's
        # request was fine.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    log.info(
        "sourcing.find_emails org=%s user=%s requested=%s summary=%s",
        principal.org_id,
        principal.user_id,
        len(payload.logins),
        result.get("summary"),
    )
    return result
