"""The discovered-contacts tray: review, promote, dismiss.

Sweep controls live on the integrations router instead, next to the other
per-mailbox actions — they operate on a connection, not on the CRM.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..duplicates import dismiss_pair as dismiss_duplicate_pair
from ..duplicates import merge as merge_candidates
from ..duplicates import suggestions as duplicate_suggestions
from ..integrations.discovery import dismiss as dismiss_row
from ..integrations.discovery import promote as promote_row
from ..models import (
    DISCOVERED_STATUSES,
    Candidate,
    CandidateTag,
    DiscoveredContact,
    Org,
    record_event,
)
from ..schemas import (
    DiscoveredBulkResult,
    DiscoveredDismissRequest,
    DiscoveredOut,
    DiscoveredPromoteRequest,
    DismissDuplicateRequest,
    IgnoreDomainRequest,
    MergeRequest,
)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/crm", tags=["crm"])

MAX_BULK_REVIEW = 500
SORTS = ("last_seen", "first_seen", "messages", "threads")


def _listing(
    *,
    org_id: str,
    status_value: str,
    q: str | None,
    min_messages: int,
    include_cc_only: bool,
):
    stmt = select(DiscoveredContact).where(
        DiscoveredContact.org_id == org_id,
        DiscoveredContact.status == status_value,
    )
    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(DiscoveredContact.email).like(pattern),
                func.lower(DiscoveredContact.display_name).like(pattern),
                func.lower(DiscoveredContact.domain).like(pattern),
            )
        )
    if min_messages > 1:
        stmt = stmt.where(DiscoveredContact.outbound_count >= min_messages)
    if not include_cc_only:
        # Someone only ever Cc'd is the weakest signal in the tray; hidden by
        # default rather than dropped, because at a small company they are
        # sometimes the actual counterparty.
        stmt = stmt.where(DiscoveredContact.suggested_status != "cc_only")
    return stmt


@router.get("/tags")
def list_tags(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[dict]:
    """The org's tag vocabulary with usage counts, for autocomplete.

    Derived from the join table rather than kept in a dictionary table: a tag
    nobody uses any more should stop being suggested on its own.
    """
    rows = db.execute(
        select(CandidateTag.tag, func.count(CandidateTag.id))
        .where(CandidateTag.org_id == principal.org_id)
        .group_by(CandidateTag.tag)
        .order_by(func.count(CandidateTag.id).desc(), CandidateTag.tag)
    ).all()
    return [{"tag": tag, "count": int(count)} for tag, count in rows]


@router.get("/duplicates")
def list_duplicates(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[dict]:
    """People who may be in the CRM twice, strongest evidence first.

    Suggestions only. Nothing is merged without someone saying so: a matching
    name is not proof, and a merge cannot be undone once notes, mail and
    enrolments have moved onto the survivor.
    """
    groups = duplicate_suggestions(db, org_id=principal.org_id)
    if not groups:
        return []

    people = {
        row.id: row
        for row in db.scalars(
            select(Candidate).where(
                Candidate.id.in_([i for g in groups for i in g.candidate_ids])
            )
        ).all()
    }
    return [
        {
            "name": group.name,
            "confidence": group.confidence,
            "reason": group.reason,
            "candidates": [
                {
                    "id": cid,
                    "full_name": people[cid].full_name,
                    "emails": people[cid].emails,
                    "current_title": people[cid].current_title,
                    "current_company": people[cid].current_company,
                    "created_at": people[cid].created_at,
                }
                for cid in group.candidate_ids
                if cid in people
            ],
        }
        for group in groups
    ]


@router.post("/duplicates/merge")
def merge_duplicates(
    payload: MergeRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    """Fold the duplicates into the chosen survivor. Not reversible."""
    result = merge_candidates(
        db,
        org_id=principal.org_id,
        primary_id=payload.primary_id,
        duplicate_ids=payload.duplicate_ids,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=result["error"]
        )
    record_event(
        db,
        org_id=principal.org_id,
        type="crm.candidates_merged",
        subject_type="candidate",
        subject_id=payload.primary_id,
        actor_id=principal.user_id,
        payload={"merged": result["merged"], "moved": result["moved"]},
    )
    return result


@router.post("/duplicates/dismiss")
def dismiss_duplicate(
    payload: DismissDuplicateRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    """Say that these are different people, so the pair stops being suggested."""
    ids = payload.candidate_ids
    if len(ids) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Dismissing needs at least two candidates",
        )
    pairs = 0
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            dismiss_duplicate_pair(
                db,
                org_id=principal.org_id,
                left=left,
                right=right,
                actor_id=principal.user_id,
            )
            pairs += 1
    return {"dismissed": pairs}


@router.get("/discovered", response_model=list[DiscoveredOut])
def list_discovered(
    status_value: str = Query(default="new", alias="status"),
    q: str | None = Query(default=None),
    min_messages: int = Query(default=1, ge=1),
    include_cc_only: bool = Query(default=False),
    sort: str = Query(default="last_seen"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[DiscoveredOut]:
    """The review tray.

    Defaults to ``new``: mixing in the people who are already in Contacts
    would bury the strangers the sweep exists to surface, so that bucket is a
    separate filter with its own count.
    """
    if status_value not in DISCOVERED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {list(DISCOVERED_STATUSES)}",
        )
    if sort not in SORTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"sort must be one of {list(SORTS)}",
        )

    stmt = _listing(
        org_id=principal.org_id,
        status_value=status_value,
        q=q,
        min_messages=min_messages,
        include_cc_only=include_cc_only,
    )
    columns = {
        "last_seen": DiscoveredContact.last_seen_at,
        "first_seen": DiscoveredContact.first_seen_at,
        "messages": DiscoveredContact.outbound_count,
        "threads": DiscoveredContact.thread_count,
    }
    rows = db.scalars(
        stmt.order_by(columns[sort].desc().nulls_last(), DiscoveredContact.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [DiscoveredOut.model_validate(row) for row in rows]


@router.get("/discovered/count")
def count_discovered(
    status_value: str = Query(default="new", alias="status"),
    q: str | None = Query(default=None),
    min_messages: int = Query(default=1, ge=1),
    include_cc_only: bool = Query(default=False),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    stmt = _listing(
        org_id=principal.org_id,
        status_value=status_value,
        q=q,
        min_messages=min_messages,
        include_cc_only=include_cc_only,
    )
    total = db.scalar(
        select(func.count()).select_from(
            stmt.with_only_columns(DiscoveredContact.id).subquery()
        )
    )
    return {"count": int(total or 0)}


def _load_rows(db: Session, *, org_id: str, ids: list[str]) -> list[DiscoveredContact]:
    if len(ids) > MAX_BULK_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Review at most {MAX_BULK_REVIEW} at a time",
        )
    return db.scalars(
        select(DiscoveredContact).where(
            DiscoveredContact.org_id == org_id, DiscoveredContact.id.in_(ids)
        )
    ).all()


@router.post("/discovered/promote", response_model=DiscoveredBulkResult)
def promote_discovered(
    payload: DiscoveredPromoteRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> DiscoveredBulkResult:
    """Turn staged contacts into real ones and import their history.

    Per-row failures are reported rather than swallowed: ``upsert_candidates``
    silently skips a payload whose name will not normalise, so a bulk promote
    could otherwise lose people and still look like it worked.
    """
    from ..jobs import KIND_CRM_DISCOVERY_IMPORT, enqueue
    from ..models import utcnow

    rows = _load_rows(db, org_id=principal.org_id, ids=payload.ids)
    promoted: list[str] = []
    failed: dict[str, str] = {}
    addresses: list[str] = []

    for row in rows:
        candidate_id, error = promote_row(
            db, row=row, org_id=principal.org_id, crm_status=payload.crm_status or ""
        )
        if candidate_id is None:
            failed[row.email] = error
            continue
        promoted.append(candidate_id)
        addresses.append(row.email)

    if addresses:
        record_event(
            db,
            org_id=principal.org_id,
            type="crm.contacts_promoted",
            subject_type="org",
            subject_id=principal.org_id,
            actor_id=principal.user_id,
            payload={"count": len(addresses)},
        )
        enqueue(
            db,
            org_id=principal.org_id,
            kind=KIND_CRM_DISCOVERY_IMPORT,
            # Timestamped, because enqueue returns a succeeded row forever and
            # a fixed key would make every promote after the first import
            # nothing at all.
            idempotency_key=(
                f"{KIND_CRM_DISCOVERY_IMPORT}:{principal.org_id}:"
                f"{int(utcnow().timestamp())}"
            ),
            payload={"org_id": principal.org_id, "addresses": addresses},
        )

    return DiscoveredBulkResult(
        promoted=len(promoted), dismissed=0, candidate_ids=promoted, failed=failed
    )


@router.post("/discovered/dismiss", response_model=DiscoveredBulkResult)
def dismiss_discovered(
    payload: DiscoveredDismissRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> DiscoveredBulkResult:
    rows = _load_rows(db, org_id=principal.org_id, ids=payload.ids)
    for row in rows:
        dismiss_row(db, row=row, reason=payload.reason or "")
    return DiscoveredBulkResult(promoted=0, dismissed=len(rows), candidate_ids=[], failed={})


@router.post("/discovered/ignore-domain", response_model=DiscoveredBulkResult)
def ignore_domain(
    payload: IgnoreDomainRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> DiscoveredBulkResult:
    """Kill a whole vendor in one click.

    Dismisses everyone on the domain now, and adds it to the org's internal
    list so the next sweep filters them at source rather than re-staging them.
    """
    domain = (payload.domain or "").strip().lower().lstrip("@")
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="domain is required"
        )

    org = db.get(Org, principal.org_id)
    existing = list(org.crm_internal_domains or [])
    if domain not in existing:
        org.crm_internal_domains = sorted([*existing, domain])

    rows = db.scalars(
        select(DiscoveredContact).where(
            DiscoveredContact.org_id == principal.org_id,
            DiscoveredContact.domain == domain,
            DiscoveredContact.status.in_(("new", "already_known")),
        )
    ).all()
    for row in rows:
        dismiss_row(db, row=row, reason="ignored domain")

    return DiscoveredBulkResult(
        promoted=0, dismissed=len(rows), candidate_ids=[], failed={}
    )
