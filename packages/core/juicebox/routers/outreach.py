"""Sequences, enrolments, sending and inbound replies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..deps import Principal, get_db, get_principal, require_write
from ..models import Candidate, Enrollment, Message, Sequence, SequenceStep
from ..outreach.sequencer import (
    enroll_candidates,
    find_message_for_reply,
    preview_step,
    record_reply,
    run_due,
)
from ..schemas import (
    EnrollmentOut,
    EnrollRequest,
    InboundEmail,
    MessageOut,
    PreviewRequest,
    PreviewResponse,
    RunSequenceResponse,
    SequenceIn,
    SequenceOut,
)

router = APIRouter(prefix="/v1/sequences", tags=["outreach"])


def _load_sequence(db: Session, sequence_id: str, org_id: str) -> Sequence:
    sequence = db.scalar(
        select(Sequence)
        .where(Sequence.id == sequence_id, Sequence.org_id == org_id)
        .options(selectinload(Sequence.steps))
    )
    if sequence is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sequence not found")
    return sequence


@router.post("", response_model=SequenceOut, status_code=status.HTTP_201_CREATED)
def create_sequence(
    payload: SequenceIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SequenceOut:
    sequence = Sequence(
        org_id=principal.org_id,
        project_id=payload.project_id,
        name=payload.name,
        personalize=payload.personalize,
        stop_on_reply=payload.stop_on_reply,
        from_email=payload.from_email,
        from_name=payload.from_name,
    )
    db.add(sequence)
    db.flush()
    for order, step in enumerate(payload.steps):
        db.add(
            SequenceStep(
                sequence_id=sequence.id,
                step_order=order,
                channel=step.channel,
                delay_days=step.delay_days,
                subject_template=step.subject_template,
                body_template=step.body_template,
            )
        )
    db.flush()
    db.refresh(sequence)
    return SequenceOut.model_validate(sequence)


@router.get("", response_model=list[SequenceOut])
def list_sequences(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[SequenceOut]:
    rows = db.scalars(
        select(Sequence)
        .where(Sequence.org_id == principal.org_id)
        .options(selectinload(Sequence.steps))
        .order_by(Sequence.created_at.desc())
    ).all()
    return [SequenceOut.model_validate(row) for row in rows]


@router.get("/{sequence_id}", response_model=SequenceOut)
def get_sequence(
    sequence_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> SequenceOut:
    return SequenceOut.model_validate(_load_sequence(db, sequence_id, principal.org_id))


@router.patch("/{sequence_id}/status", response_model=SequenceOut)
def set_status(
    sequence_id: str,
    value: str = Query(pattern="^(active|paused|archived)$"),
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SequenceOut:
    sequence = _load_sequence(db, sequence_id, principal.org_id)
    sequence.status = value
    db.flush()
    return SequenceOut.model_validate(sequence)


@router.post("/{sequence_id}/preview", response_model=PreviewResponse)
def preview(
    sequence_id: str,
    payload: PreviewRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> PreviewResponse:
    sequence = _load_sequence(db, sequence_id, principal.org_id)
    candidate = db.scalar(
        select(Candidate).where(
            Candidate.id == payload.candidate_id, Candidate.org_id == principal.org_id
        )
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    subject, body, context = preview_step(
        db, sequence=sequence, candidate=candidate, step_order=payload.step_order
    )
    return PreviewResponse(subject=subject, body=body, personalization=context)


@router.post("/{sequence_id}/enroll", response_model=list[EnrollmentOut])
def enroll(
    sequence_id: str,
    payload: EnrollRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> list[EnrollmentOut]:
    sequence = _load_sequence(db, sequence_id, principal.org_id)
    enrollments = enroll_candidates(
        db,
        sequence=sequence,
        candidate_ids=payload.candidate_ids,
        project_id=payload.project_id,
        start_immediately=payload.start_immediately,
    )
    return [EnrollmentOut.model_validate(e) for e in enrollments]


@router.get("/{sequence_id}/enrollments", response_model=list[EnrollmentOut])
def list_enrollments(
    sequence_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[EnrollmentOut]:
    _load_sequence(db, sequence_id, principal.org_id)
    rows = db.scalars(
        select(Enrollment)
        .where(Enrollment.sequence_id == sequence_id)
        .order_by(Enrollment.created_at.desc())
    ).all()
    return [EnrollmentOut.model_validate(row) for row in rows]


@router.post("/{sequence_id}/run", response_model=RunSequenceResponse)
def run_sequence(
    sequence_id: str,
    dry_run: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1, le=1000),
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> RunSequenceResponse:
    _load_sequence(db, sequence_id, principal.org_id)
    summary = run_due(
        db,
        org_id=principal.org_id,
        sequence_id=sequence_id,
        limit=limit,
        dry_run=dry_run,
    )
    return RunSequenceResponse(
        processed=summary.processed,
        sent=summary.sent,
        failed=summary.failed,
        completed=summary.completed,
        skipped=summary.skipped,
        messages=[MessageOut.model_validate(m) for m in summary.messages],
    )


# --------------------------------------------------------------------------
# messages + inbound
# --------------------------------------------------------------------------

messages_router = APIRouter(prefix="/v1/messages", tags=["outreach"])


@messages_router.get("", response_model=list[MessageOut])
def list_messages(
    candidate_id: str | None = None,
    direction: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MessageOut]:
    stmt = select(Message).where(Message.org_id == principal.org_id)
    if candidate_id:
        stmt = stmt.where(Message.candidate_id == candidate_id)
    if direction:
        stmt = stmt.where(Message.direction == direction)
    rows = db.scalars(stmt.order_by(Message.created_at.desc()).limit(limit)).all()
    return [MessageOut.model_validate(row) for row in rows]


webhooks_router = APIRouter(prefix="/v1/webhooks", tags=["outreach"])


@webhooks_router.post("/inbound-email")
def inbound_email(
    payload: InboundEmail,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict:
    """Point your ESP's inbound-parse webhook here (authenticate with an API key)."""
    sender = (payload.candidate_email or payload.from_address or "").strip().lower()
    message = find_message_for_reply(
        db,
        org_id=principal.org_id,
        candidate_email=sender,
        provider_message_id=payload.in_reply_to,
    )
    if message is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No outbound message matched this reply",
        )
    verdict = record_reply(db, message=message, subject=payload.subject, body=payload.body)
    return {
        "message_id": message.id,
        "candidate_id": message.candidate_id,
        "intent": verdict.intent,
        "confidence": verdict.confidence,
        "stopped_sequence": verdict.should_stop_sequence,
        "classified_by": verdict.classified_by,
        "summary": verdict.summary,
    }
