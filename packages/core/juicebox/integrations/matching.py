"""Which mailbox traffic belongs in the CRM: the in-scope address index.

The privacy guard for thread sync lives here. An email is only ingested when
it resolves to an *in-scope* candidate — someone deliberately put into a
project pipeline or a sequence, or already in correspondence. The recruiter's
bosses, vendors and family never match, so their mail is never stored.

``Candidate.emails`` is a JSON list, so this cannot be a SQL join; the index
is built once per sync pass (a filtered select expanded in Python) and shared
across every message in that pass.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ..models import Candidate, Enrollment, Message, ProjectCandidate


def build_address_index(db: Session, *, org_id: str) -> dict[str, str]:
    """Lowercased email -> candidate id, for in-scope candidates only."""
    in_scope = (
        select(Candidate)
        .where(
            Candidate.org_id == org_id,
            exists(select(Enrollment.id).where(Enrollment.candidate_id == Candidate.id))
            | exists(
                select(ProjectCandidate.id).where(
                    ProjectCandidate.candidate_id == Candidate.id
                )
            )
            | exists(select(Message.id).where(Message.candidate_id == Candidate.id)),
        )
    )
    index: dict[str, str] = {}
    for candidate in db.scalars(in_scope):
        for email in candidate.emails or []:
            address = (email or "").strip().lower()
            if address:
                # First writer wins: a shared address stays with the candidate
                # created earliest rather than flapping between two people.
                index.setdefault(address, candidate.id)
    return index


def resolve_candidate(index: dict[str, str], addresses: list[str]) -> str | None:
    for address in addresses:
        candidate_id = index.get((address or "").strip().lower())
        if candidate_id:
            return candidate_id
    return None


def record_synced_message(
    db: Session,
    *,
    org_id: str,
    candidate_id: str,
    mailbox_id: str,
    direction: str,
    from_address: str,
    to_address: str,
    payload: dict,
    now: datetime,
) -> Message:
    """One Outlook message -> one CRM row. No enrollment, no side effects:
    classification, sequence stops and pipeline moves belong to record_reply,
    and an enrollment-less email has no sequence to stop."""
    body = payload.get("body") or {}
    content = body.get("content") or payload.get("bodyPreview") or ""
    is_html = (body.get("contentType") or "").lower() == "html"
    sent_raw = payload.get("sentDateTime") or payload.get("receivedDateTime") or ""
    try:
        sent_at = datetime.fromisoformat(sent_raw.replace("Z", "+00:00")) if sent_raw else now
    except ValueError:
        sent_at = now

    message = Message(
        org_id=org_id,
        enrollment_id=None,
        candidate_id=candidate_id,
        step_id=None,
        channel="email",
        direction=direction,
        source="outlook",
        status="sent" if direction == "outbound" else "received",
        from_address=from_address,
        to_address=to_address,
        subject=payload.get("subject", "") or "",
        body="" if is_html else content,
        body_html=content if is_html else "",
        mailbox_id=mailbox_id,
        graph_message_id=payload.get("id", "") or "",
        internet_message_id=payload.get("internetMessageId", "") or "",
        conversation_id=payload.get("conversationId", "") or "",
        web_link=payload.get("webLink", "") or "",
        attachment_count=1 if payload.get("hasAttachments") else 0,
        sent_at=sent_at,
    )
    db.add(message)
    db.flush()
    return message
