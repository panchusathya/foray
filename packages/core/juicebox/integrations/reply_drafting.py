"""When a candidate replies, put a ready-to-edit response in the user's Drafts.

The Juicebox loop: reply detected (sync.py) → this module writes a reply draft
on the actual Outlook thread via Graph createReply, then emails the user a
"your draft is ready" notification from their own mailbox. The user edits and
sends from Outlook itself — we never send the reply for them.

Drafting uses the LLM when configured and a plain per-intent template
otherwise, so the loop works offline end to end. What the user eventually
sends (vs. what we drafted) is Phase 4.5's learning signal — the ReplyDraft
row stores our version for that comparison.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from ..config import get_settings
from ..llm.client import get_llm
from ..llm.prompts import REPLY_DRAFT_SCHEMA, REPLY_DRAFT_SYSTEM, reply_draft_prompt
from ..models import (
    Candidate,
    Enrollment,
    MailboxConnection,
    Message,
    Project,
    ReplyDraft,
    record_event,
    utcnow,
)
from ..outreach.templates import to_html
from .microsoft import GraphClient

logger = logging.getLogger(__name__)

# Automated or opt-out mail never gets a drafted response.
NO_DRAFT_INTENTS = frozenset({"bounce", "out_of_office", "unsubscribe"})

_TEMPLATE_BY_INTENT = {
    "interested": (
        "Thanks for getting back to me, {first_name} — glad this looks interesting.\n\n"
        "Happy to walk you through the team, the work, and the process on a short "
        "call. Could you share a couple of times that work this week or next?"
    ),
    "question": (
        "Thanks for the reply, {first_name} — good questions.\n\n"
        "I'll put the specifics together and come back to you shortly. If it's "
        "easier, I can also cover everything on a short call — happy either way."
    ),
    "referral": (
        "Thanks {first_name}, I appreciate the pointer.\n\n"
        "If you're comfortable making a quick intro, that would be great — "
        "otherwise I'm happy to reach out directly and mention you suggested it."
    ),
    "not_now": (
        "Understood, {first_name} — thanks for letting me know.\n\n"
        "When would be a good time to check back in? I'll make a note and won't "
        "bother you before then."
    ),
    "not_interested": (
        "Thanks for the straight answer, {first_name} — I won't take more of "
        "your time.\n\nIf anything changes down the line, my inbox is open."
    ),
}
_TEMPLATE_DEFAULT = (
    "Thanks for getting back to me, {first_name}.\n\n"
    "Happy to share more or answer anything — would a short call this week work?"
)


def _candidate_context(candidate: Candidate | None) -> str:
    if candidate is None:
        return "Unknown candidate"
    parts = [candidate.full_name]
    if getattr(candidate, "current_title", ""):
        parts.append(str(candidate.current_title))
    if getattr(candidate, "current_company", ""):
        parts.append(f"at {candidate.current_company}")
    return " — ".join(parts)


def _role_context(db, inbound: Message) -> str:
    enrollment = db.get(Enrollment, inbound.enrollment_id)
    if enrollment is None:
        return ""
    parts = []
    sequence = enrollment.sequence
    if sequence is not None:
        parts.append(f"Sequence: {sequence.name}")
    if enrollment.project_id:
        project = db.get(Project, enrollment.project_id)
        if project is not None:
            parts.append(f"Role: {project.role_title or project.name}")
            if project.location:
                parts.append(f"Location: {project.location}")
            if project.description:
                parts.append(f"Description: {project.description}")
    return "\n".join(parts)


def _first_name(candidate: Candidate | None) -> str:
    if candidate is None or not candidate.full_name:
        return "there"
    return candidate.full_name.split()[0]


def _template_body(intent: str, candidate: Candidate | None) -> str:
    template = _TEMPLATE_BY_INTENT.get(intent, _TEMPLATE_DEFAULT)
    return template.format(first_name=_first_name(candidate))


def recent_style_examples(db, *, mailbox_id: str, limit: int = 3) -> str:
    """The user's latest sent/rewritten drafts, formatted for the prompt.

    Sent means they kept (an edit of) our draft; discarded means they wrote
    their own — the strongest correction. Newest first, small on purpose: a
    few real examples steer voice better than a pile.
    """
    rows = db.scalars(
        select(ReplyDraft)
        .where(
            ReplyDraft.mailbox_id == mailbox_id,
            ReplyDraft.status.in_(("sent", "discarded")),
            ReplyDraft.final_sent_text != "",
        )
        .order_by(ReplyDraft.sent_at.desc())
        .limit(limit)
    ).all()
    blocks = []
    for row in rows:
        blocks.append(
            f'<example outcome="{row.status}">\n'
            f"<drafted_for_them>\n{row.body.strip()}\n</drafted_for_them>\n"
            f"<what_they_sent>\n{row.final_sent_text.strip()}\n</what_they_sent>\n"
            "</example>"
        )
    return "\n".join(blocks)


def generate_reply_body(
    db, *, inbound: Message, candidate: Candidate | None, mailbox_id: str = ""
) -> tuple[str, str]:
    """Return (plain-text body, generated_by)."""
    llm = get_llm()
    if llm.available:
        outbound = db.scalar(
            select(Message).where(
                Message.enrollment_id == inbound.enrollment_id,
                Message.direction == "outbound",
            ).order_by(Message.created_at.desc())
        )
        try:
            result = llm.json(
                system=REPLY_DRAFT_SYSTEM,
                prompt=reply_draft_prompt(
                    candidate_context=_candidate_context(candidate),
                    role_context=_role_context(db, inbound),
                    outbound_body=(outbound.body if outbound else ""),
                    reply_body=inbound.body,
                    intent=inbound.reply_intent,
                    style_examples=(
                        recent_style_examples(db, mailbox_id=mailbox_id) if mailbox_id else ""
                    ),
                ),
                schema=REPLY_DRAFT_SCHEMA,
                effort="medium",
                max_tokens=2000,
            )
            body = str((result.data or {}).get("body", "")).strip()
            if body:
                return body, "llm"
        except Exception as exc:  # noqa: BLE001 - fall back rather than skip the draft
            logger.warning("reply drafting failed, using template: %s", exc)
    return _template_body(inbound.reply_intent, candidate), "template"


def _send_notification(
    db,
    *,
    connection: MailboxConnection,
    client: GraphClient,
    candidate: Candidate | None,
    inbound: Message,
    draft: ReplyDraft,
) -> None:
    """'Your draft is ready' email, sent from the user's mailbox to themselves."""
    name = candidate.full_name if candidate else inbound.to_address
    subject = f"{name} replied ({inbound.reply_intent.replace('_', ' ')}) — your draft is ready"
    preview = (inbound.body or "").strip()
    if len(preview) > 400:
        preview = preview[:400] + "…"
    app_url = get_settings().base_url.rstrip("/")
    text_lines = [
        f"{name} replied to your outreach.",
        "",
        f"Intent: {inbound.reply_intent}",
        "",
        "Their reply:",
        preview,
        "",
        "A response draft is waiting in your Outlook Drafts — edit and send:",
        draft.web_link or "https://outlook.office.com/mail/drafts",
        "",
        f"Candidate in Foray: {app_url}/contacts",
    ]
    body_text = "\n".join(text_lines)
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": to_html(body_text)},
        "toRecipients": [{"emailAddress": {"address": connection.address}}],
    }
    try:
        created = client.create_draft(message)
        client.send_draft(created["id"])
    except Exception as exc:  # noqa: BLE001 - the draft matters more than the ping
        logger.warning("draft-ready notification failed for %s: %s", connection.address, exc)


def create_reply_draft(
    db,
    *,
    inbound: Message,
    connection: MailboxConnection,
    client: GraphClient | None = None,
    now: datetime | None = None,
) -> ReplyDraft | None:
    """Draft a response to one inbound reply. Idempotent per inbound message."""
    now = now or utcnow()
    if inbound.direction != "inbound" or inbound.reply_intent in NO_DRAFT_INTENTS:
        return None
    existing = db.scalar(
        select(ReplyDraft).where(ReplyDraft.inbound_message_id == inbound.id)
    )
    if existing is not None:
        return existing
    if not inbound.graph_message_id:
        # Replies that arrived via webhook instead of sync have no thread to
        # attach a Graph draft to.
        return None

    client = client or GraphClient(db, connection)
    candidate = db.get(Candidate, inbound.candidate_id) if inbound.candidate_id else None
    body, generated_by = generate_reply_body(
        db, inbound=inbound, candidate=candidate, mailbox_id=connection.id
    )
    graph_draft = client.create_reply_draft(inbound.graph_message_id, to_html(body))

    draft = ReplyDraft(
        org_id=inbound.org_id,
        mailbox_id=connection.id,
        inbound_message_id=inbound.id,
        candidate_id=inbound.candidate_id,
        graph_draft_id=graph_draft.get("id", ""),
        web_link=graph_draft.get("webLink", ""),
        body=body,
        generated_by=generated_by,
        intent=inbound.reply_intent,
    )
    db.add(draft)
    db.flush()
    record_event(
        db,
        org_id=inbound.org_id,
        type="outreach.draft_ready",
        subject_type="reply_draft",
        subject_id=draft.id,
        payload={
            "candidate_id": inbound.candidate_id,
            "intent": inbound.reply_intent,
            "generated_by": generated_by,
        },
    )
    _send_notification(
        db,
        connection=connection,
        client=client,
        candidate=candidate,
        inbound=inbound,
        draft=draft,
    )
    return draft
