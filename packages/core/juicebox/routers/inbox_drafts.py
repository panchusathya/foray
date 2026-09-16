"""The inbox draft queue: read, edit, approve, dismiss.

Approving is the only call that writes to the mailbox, and it is the only one
the user cannot undo from here -- once a draft is on the thread it belongs to
their mail client. Everything else stays inside the app.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import CommittingRoute, Principal, get_db, get_principal
from ..integrations.inbox_drafting import place_in_mailbox
from ..integrations.providers import ProviderError
from ..models import Candidate, InboxDraft, record_event, utcnow
from ..schemas import (
    InboxDraftListOut,
    InboxDraftOut,
    InboxDraftUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/inbox-drafts", tags=["inbox-drafts"])

# The queue is a working list, not an archive.
_MAX_LIMIT = 200


def _out(draft: InboxDraft, candidate: Candidate | None) -> InboxDraftOut:
    return InboxDraftOut(
        id=draft.id,
        mailbox_id=draft.mailbox_id,
        status=draft.status,
        from_address=draft.from_address,
        from_name=draft.from_name,
        subject=draft.subject,
        inbound_body=draft.inbound_body,
        received_at=draft.received_at,
        web_link=draft.web_link,
        body=draft.body,
        generated_by=draft.generated_by,
        # An empty body on a draft we have already written is the model saying
        # this one deserves a person; an empty body before that is just work
        # not done yet. The UI shows those very differently.
        needs_a_human=bool(draft.generated_by) and not draft.body.strip(),
        drafting=not draft.generated_by,
        edited=bool(draft.generated_by) and draft.body != draft.generated_body,
        candidate_id=draft.candidate_id,
        candidate_name=(candidate.full_name if candidate else ""),
        draft_web_link=draft.draft_web_link,
        error=draft.error,
        created_at=draft.created_at,
    )


def _load(db: Session, draft_id: str, org_id: str) -> InboxDraft:
    draft = db.scalar(
        select(InboxDraft).where(InboxDraft.id == draft_id, InboxDraft.org_id == org_id)
    )
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
    return draft


@router.get("", response_model=InboxDraftListOut)
def list_drafts(
    status_filter: str = "pending",
    limit: int = 50,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> InboxDraftListOut:
    query = select(InboxDraft).where(InboxDraft.org_id == principal.org_id)
    if status_filter and status_filter != "all":
        query = query.where(InboxDraft.status == status_filter)
    rows = db.scalars(
        query.order_by(InboxDraft.received_at.desc().nullslast(), InboxDraft.created_at.desc())
        .limit(min(max(limit, 1), _MAX_LIMIT))
    ).all()

    # One lookup for the whole page rather than a lazy load per row.
    names = {
        c.id: c
        for c in db.scalars(
            select(Candidate).where(
                Candidate.id.in_([r.candidate_id for r in rows if r.candidate_id])
            )
        )
    }
    pending = db.scalar(
        select(func.count(InboxDraft.id)).where(
            InboxDraft.org_id == principal.org_id, InboxDraft.status == "pending"
        )
    )
    return InboxDraftListOut(
        drafts=[_out(row, names.get(row.candidate_id or "")) for row in rows],
        pending_count=int(pending or 0),
    )


@router.patch("/{draft_id}", response_model=InboxDraftOut)
def update_draft(
    draft_id: str,
    payload: InboxDraftUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> InboxDraftOut:
    draft = _load(db, draft_id, principal.org_id)
    if draft.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Draft is already {draft.status}",
        )
    if payload.body is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Nothing to update"
        )
    draft.body = payload.body
    db.flush()
    return _out(draft, db.get(Candidate, draft.candidate_id) if draft.candidate_id else None)


@router.post("/{draft_id}/approve", response_model=InboxDraftOut)
def approve_draft(
    draft_id: str,
    payload: InboxDraftUpdate | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> InboxDraftOut:
    """Place the draft on the real thread. The user still sends it themselves."""
    draft = _load(db, draft_id, principal.org_id)
    if draft.status == "placed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Draft is already in the mailbox"
        )
    if draft.status == "dismissed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Draft was dismissed")
    # Approving with a body is the common path: the user edits and approves in
    # one action, and a separate PATCH would leave a window where a retry sends
    # the old text.
    if payload is not None and payload.body is not None:
        draft.body = payload.body
    if not draft.body.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Write the reply before approving it",
        )
    draft.reviewed_by = principal.user_id
    try:
        place_in_mailbox(db, draft=draft)
    except ProviderError as exc:
        # Left pending on purpose. Marking it placed when nothing reached the
        # mailbox would lose the reply with no sign anything went wrong.
        draft.error = str(exc)
        # Committed here rather than left to CommittingRoute, which only runs
        # when the handler returns: raising below would roll this back, and the
        # reason would survive only in the 502 the user is about to navigate
        # away from. Safe to commit -- nothing else in this request wrote.
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not put the draft in your mailbox: {exc}",
        ) from exc
    return _out(draft, db.get(Candidate, draft.candidate_id) if draft.candidate_id else None)


@router.post("/{draft_id}/dismiss", response_model=InboxDraftOut)
def dismiss_draft(
    draft_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> InboxDraftOut:
    draft = _load(db, draft_id, principal.org_id)
    if draft.status == "placed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Draft is already in the mailbox",
        )
    draft.status = "dismissed"
    draft.reviewed_at = utcnow()
    draft.reviewed_by = principal.user_id
    db.flush()
    record_event(
        db,
        org_id=principal.org_id,
        type="inbox_draft.dismissed",
        subject_type="inbox_draft",
        subject_id=draft.id,
        actor_id=principal.user_id,
        payload={},
    )
    return _out(draft, db.get(Candidate, draft.candidate_id) if draft.candidate_id else None)
