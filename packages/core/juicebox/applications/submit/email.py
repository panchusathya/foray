"""Tier 1: postings that take an application by email.

The smallest real submission there is, and it reuses ``outreach/sender.py``
wholesale -- the same providers, the same console default, the same
``SendResult``. What it does not reuse is the outreach *identity*: outreach is
a named recruiter writing to a candidate, and this is a candidate applying for
a job. The ``From`` is the candidate's alias and the reply lands back in the
mailbox we own, which is what lets the tracker see the employer's answer.
"""

from __future__ import annotations

import logging

from ...outreach.sender import OutboundEmail, get_sender
from .base import METHOD_EMAIL, SubmissionContext, SubmissionResult

logger = logging.getLogger(__name__)

#: Enough for a recruiter to file it without opening the attachments.
SUBJECT = "Application: {title}"


def build_email(ctx: SubmissionContext) -> OutboundEmail:
    address = ctx.target.apply_url.removeprefix("mailto:").strip()
    lines = [
        f"Hello {ctx.company or 'there'},",
        "",
        f"I'd like to apply for {ctx.title or 'the open role'}"
        + (f" at {ctx.company}" if ctx.company else "")
        + ".",
    ]
    if ctx.posting_url:
        lines.append(f"Posting: {ctx.posting_url}")
    # Only answers that exist. A blank comp expectation is a blank we chose not
    # to invent (docgen/answers.py), and printing "Salary expectation:" with
    # nothing after it reads worse than leaving it out.
    stated = [
        (entry.get("question") or entry.get("key") or "", str(entry.get("answer") or "").strip())
        for entry in ctx.answers
    ]
    answered = [(question, answer) for question, answer in stated if question and answer]
    if answered:
        lines.append("")
        for question, answer in answered:
            lines.append(f"{question} {answer}")
    lines += ["", "My resume and cover letter are attached.", "", ctx.full_name or ""]
    if ctx.phone:
        lines.append(ctx.phone)
    if ctx.linkedin_url:
        lines.append(ctx.linkedin_url)

    return OutboundEmail(
        to=address,
        subject=SUBJECT.format(title=ctx.title or "open role"),
        text_body="\n".join(lines).strip() + "\n",
        html_body="",
        from_email=ctx.from_address,
        from_name=ctx.full_name,
        headers={"Reply-To": ctx.from_address} if ctx.from_address else {},
    )


class EmailSubmitter:
    """Sends the package to the address the posting named."""

    vendor = "email"
    method = METHOD_EMAIL

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        address = ctx.target.apply_url.removeprefix("mailto:").strip()
        if "@" not in address:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error=f"no application address to send to ({ctx.target.apply_url!r})",
            )
        if not ctx.from_address:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error="no from address: applying without a return address is not a thing",
            )
        if ctx.resume is None:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error="no resume in the package",
            )

        # The providers in outreach/sender.py have no attachment channel, and
        # an application email without the resume is worse than none. SMTP is
        # the only one that can carry it today, so anything else is logged and
        # reported as needing a human rather than sent half-formed.
        email = build_email(ctx)
        sender = get_sender()
        attach = getattr(sender, "send_with_attachments", None)
        if callable(attach):
            result = attach(email, ctx.attachments)
        else:
            result = sender.send(email)
            if getattr(sender, "name", "") not in {"console", "memory"}:
                logger.warning(
                    "%s cannot carry attachments; application %s went without them",
                    getattr(sender, "name", "?"),
                    ctx.application_id,
                )
        if not result.ok:
            return SubmissionResult(ok=False, method=self.method, error=result.error)
        return SubmissionResult(
            ok=True,
            method=self.method,
            external_id=result.provider_message_id or result.internet_message_id,
            confirmation_ref=f"emailed {address}",
        )
