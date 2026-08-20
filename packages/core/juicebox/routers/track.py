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
from ..security import verify_tracking_token

router = APIRouter(
    route_class=CommittingRoute, prefix="/t", tags=["tracking"], include_in_schema=False
)

# 1x1 transparent GIF.
_PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


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
    parsed = urlparse(u)
    safe_target = u if parsed.scheme in {"http", "https"} and parsed.netloc else "/"

    if verify_tracking_token(message_id, k):
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
