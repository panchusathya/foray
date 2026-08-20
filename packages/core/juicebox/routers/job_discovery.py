"""Conversational job discovery. Stateless — the client posts the thread."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..deps import CommittingRoute, Principal, get_db, require_write
from ..jobmatch.discover import discover_turn
from ..schemas import DiscoverTurnIn, DiscoverTurnOut

router = APIRouter(
    route_class=CommittingRoute, prefix="/v1/job-discovery", tags=["job-discovery"]
)


@router.post("/turns", response_model=DiscoverTurnOut)
def post_turn(
    payload: DiscoverTurnIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> DiscoverTurnOut:
    return discover_turn(
        db,
        principal.org_id,
        payload.messages,
        num_results=payload.num_results,
    )
