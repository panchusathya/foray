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
    )


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
    if payload.paused:
        if org.outreach_paused_at is None:
            org.outreach_paused_at = utcnow()
            org.outreach_paused_by = principal.user_id
    else:
        org.outreach_paused_at = None
        org.outreach_paused_by = None
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
