"""Workspace settings and the do-not-contact list."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..models import Org, Suppression, User, utcnow
from ..outreach.outbox import held_count
from ..outreach.suppressions import suppress, unsuppress
from ..schemas import (
    OrgOut,
    OrgPatch,
    OrgSendingOut,
    OrgSendingPatch,
    SuppressionIn,
    SuppressionOut,
)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/org", tags=["org"])


def _org(db: Session, org_id: str) -> Org:
    org = db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Org not found")
    return org


@router.get("", response_model=OrgOut)
def get_org(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> OrgOut:
    return OrgOut.model_validate(_org(db, principal.org_id))


@router.patch("", response_model=OrgOut)
def patch_org(
    payload: OrgPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> OrgOut:
    org = _org(db, principal.org_id)
    if payload.name is not None:
        org.name = payload.name
    db.flush()
    return OrgOut.model_validate(org)


def _sending_out(db, org: Org) -> OrgSendingOut:
    paused_by_email = None
    if org.outreach_paused_by:
        user = db.get(User, org.outreach_paused_by)
        paused_by_email = user.email if user is not None else None
    return OrgSendingOut(
        paused=org.outreach_paused_at is not None,
        paused_at=org.outreach_paused_at,
        paused_by_email=paused_by_email,
        held_count=held_count(db, org_id=org.id),
        send_priority=org.send_priority or "first_emails",
        **_pacing_out(db, org),
    )


def _pacing_out(db, org: Org) -> dict:
    from ..outreach.rotation import gap_seconds
    from ..outreach.window import closes_at, next_open, org_window

    now = utcnow()
    window = org_window(org)
    opens = next_open(window, now)
    low, _high = gap_seconds(db, org.id)
    return {
        "send_gap_enabled": org.send_gap_enabled is not False,
        # The number in force, so the field shows what is actually used.
        "send_gap_seconds": org.send_gap_seconds if org.send_gap_seconds is not None else low or 40,
        "send_gap_is_default": org.send_gap_seconds is None,
        "send_window_enabled": bool(org.send_window_enabled),
        "send_window_start": org.send_window_start or "09:00",
        "send_window_end": org.send_window_end or "17:00",
        "send_window_tz": org.send_window_tz or "America/Los_Angeles",
        "send_window_days": sorted(org.send_window_days or []),
        "send_window_breaks": list(org.send_window_breaks or []),
        "send_window_open_now": opens <= now,
        "send_window_next_change": (closes_at(window, now) if opens <= now else opens)
        if window.enabled
        else None,
    }


@router.get("/sending", response_model=OrgSendingOut)
def get_sending(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> OrgSendingOut:
    return _sending_out(db, _org(db, principal.org_id))


@router.patch("/sending", response_model=OrgSendingOut)
def patch_sending(
    payload: OrgSendingPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> OrgSendingOut:
    org = _org(db, principal.org_id)
    if payload.paused is True:
        if org.outreach_paused_at is None:
            org.outreach_paused_at = utcnow()
            org.outreach_paused_by = principal.user_id
    elif payload.paused is False:
        org.outreach_paused_at = None
        org.outreach_paused_by = None
    if payload.send_gap_enabled is not None:
        org.send_gap_enabled = payload.send_gap_enabled
    if payload.send_gap_seconds is not None:
        org.send_gap_seconds = payload.send_gap_seconds
    if payload.send_window_enabled is not None:
        org.send_window_enabled = payload.send_window_enabled
    if payload.send_window_start is not None:
        org.send_window_start = payload.send_window_start
    if payload.send_window_end is not None:
        org.send_window_end = payload.send_window_end
    if payload.send_window_tz is not None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(payload.send_window_tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(status_code=422, detail="Unknown timezone") from None
        org.send_window_tz = payload.send_window_tz
    if payload.send_window_days is not None:
        days = sorted({d for d in payload.send_window_days if 0 <= d <= 6})
        if not days:
            raise HTTPException(status_code=422, detail="Pick at least one sending day")
        org.send_window_days = days
    if payload.send_window_breaks is not None:
        breaks = sorted(
            ({"start": b.start, "end": b.end} for b in payload.send_window_breaks),
            key=lambda b: b["start"],
        )
        for b in breaks:
            if b["start"] >= b["end"]:
                raise HTTPException(status_code=422, detail="A break must end after it starts")
        org.send_window_breaks = breaks
    if (payload.send_window_start or payload.send_window_end) and (
        (org.send_window_start or "09:00") == (org.send_window_end or "17:00")
    ):
        raise HTTPException(status_code=422, detail="Start and end must differ")
    if payload.send_priority is not None and payload.send_priority != org.send_priority:
        from ..outreach.sequencer import resort_line

        org.send_priority = payload.send_priority
        db.flush()
        resort_line(db, org.id, utcnow())
    db.flush()
    return _sending_out(db, org)


suppressions_router = APIRouter(
    route_class=CommittingRoute, prefix="/v1/suppressions", tags=["org"]
)


@suppressions_router.get("", response_model=list[SuppressionOut])
def list_suppressions(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[SuppressionOut]:
    rows = db.scalars(
        select(Suppression)
        .where(Suppression.org_id == principal.org_id)
        .order_by(Suppression.created_at.desc())
    ).all()
    return [SuppressionOut.model_validate(row) for row in rows]


@suppressions_router.post("", response_model=SuppressionOut, status_code=status.HTTP_201_CREATED)
def add_suppression(
    payload: SuppressionIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SuppressionOut:
    row = suppress(
        db,
        org_id=principal.org_id,
        email=str(payload.email),
        reason=payload.reason,
        source="api",
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="email is empty")
    return SuppressionOut.model_validate(row)


@suppressions_router.delete("")
def remove_suppression(
    email: str = Query(min_length=3),
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    removed = unsuppress(db, org_id=principal.org_id, email=email)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not on the list")
    return {"removed": email}
