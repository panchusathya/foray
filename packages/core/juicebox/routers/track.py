"""Open-pixel and click-redirect endpoints.

Unauthenticated by design (an email client can't send a bearer token), so every
message id is paired with an HMAC so the endpoints can't be walked.
"""

from __future__ import annotations

import base64
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..deps import CommittingRoute, get_db
from ..models import Message, record_event, utcnow
from ..security import verify_intake_token, verify_tracking_token

router = APIRouter(
    route_class=CommittingRoute, prefix="/t", tags=["tracking"], include_in_schema=False
)

# 1x1 transparent GIF.
_PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


#: Purpose prefix for a click token that commits to the destination as well as
#: the message: ``signed_intake_token(f"click:{message_id}", target)``.
#: ``signed_tracking_token`` covers the message id alone, so a token good for
#: one link is good for ANY link — the verification half of the fix lands
#: first, and closes the moment ``templates.add_tracking`` mints this shape for
#: the ``u`` it is about to write.
_CLICK_PURPOSE = "click"


def _verify_click(message_id: str, url: str, token: str) -> bool:
    """Is this click ours? Two token shapes, for exactly as long as it takes.

    The url-bound shape names the destination inside the signature. The legacy
    shape covers the message id alone and is what every link already sitting in
    a recipient's inbox carries, so it stays accepted or real mail breaks —
    which means a legacy token still permits any destination on that message.
    """
    if verify_intake_token(f"{_CLICK_PURPOSE}:{message_id}", url, token):
        return True
    return verify_tracking_token(message_id, token)


def _pixel_response() -> Response:
    return Response(
        content=_PIXEL,
        media_type="image/gif",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, private"},
    )


@router.get("/o/{message_id}.png")
def track_open(
    message_id: str,
    k: str = Query(default=""),
    db: Session = Depends(get_db),
) -> Response:
    if not verify_tracking_token(message_id, k):
        return _pixel_response()
    message = db.get(Message, message_id)
    if message is not None and message.direction == "outbound":
        message.open_count += 1
        if message.opened_at is None:
            message.opened_at = utcnow()
            record_event(
                db,
                org_id=message.org_id,
                type="outreach.opened",
                subject_type="message",
                subject_id=message.id,
                payload={"candidate_id": message.candidate_id},
            )
    return _pixel_response()


@router.get("/c/{message_id}")
def track_click(
    message_id: str,
    u: str = Query(default=""),
    k: str = Query(default=""),
    db: Session = Depends(get_db),
) -> Response:
    # Nothing is read off `u` until the token is checked, and an unverified
    # request goes home rather than wherever it asked. This endpoint takes no
    # auth and nginx serves it from the same origin as the app, so a Location
    # we did not sign is a phishing lure wearing our own domain.
    if not _verify_click(message_id, u, k):
        return RedirectResponse(url="/", status_code=302)

    parsed = urlparse(u)
    # Absolute http(s) only: "javascript:" and friends never leave here.
    safe_target = u if parsed.scheme in {"http", "https"} and parsed.netloc else "/"

    message = db.get(Message, message_id)
    if message is not None and message.direction == "outbound":
        message.click_count += 1
        if message.clicked_at is None:
            message.clicked_at = utcnow()
        if message.opened_at is None:
            message.opened_at = utcnow()
        record_event(
            db,
            org_id=message.org_id,
            type="outreach.clicked",
            subject_type="message",
            subject_id=message.id,
            payload={"url": safe_target, "candidate_id": message.candidate_id},
        )
    return RedirectResponse(url=safe_target, status_code=302)
