"""Is this inbound message worth drafting a reply to?

The queue is only useful if it holds mail a human actually has to answer. Every
receipt, digest and build notification that lands in it is one more thing to
skim past, and after enough of them nobody opens the queue at all -- the same
failure ``discovery_filters`` was written for, which is why the vocabulary of
"never a person" prefixes and bulk-sender domains is imported from there rather
than written out a second time.

The bias is the opposite of a spam filter's. Dropping a real person's email
costs a reply that never gets written, and the user never finds out; keeping a
newsletter costs one line to skim. So a sender is only refused on a positive
signal that it is a robot, never on the absence of a signal that it is human.

No raw headers are available here -- ``MailboxEvent`` carries the parsed fields
both providers agree on, and neither surfaces ``List-Unsubscribe`` or
``Auto-Submitted`` -- so the tests are on the address, the subject and the
opening of the body.
"""

from __future__ import annotations

import re

from .discovery_filters import (
    AUTOMATED_DOMAIN_MARKERS,
    HARD_REJECT_PREFIXES,
    domain_of,
)

# Sent by a machine on a schedule, whoever the from-address claims to be.
_BULK_LOCALS = (
    "notification",
    "notifications",
    "newsletter",
    "digest",
    "alerts",
    "alert",
    "updates",
    "noreply",
    "automated",
    "mailer",
    "unsubscribe",
    "receipts",
    "invoice",
    "invoices",
    "billing",
)

# Subjects that announce the message answers itself.
_AUTOMATED_SUBJECT = re.compile(
    r"\b(out of (the )?office|automatic reply|auto[- ]?reply|autoreply"
    r"|undeliverable|delivery (status notification|has failed|failure)"
    r"|returned mail|mail delivery (failed|subsystem)"
    r"|unsubscribe|do not reply"
    r"|your (receipt|invoice|order|subscription|statement)"
    r"|password reset|verify your (email|account)|confirm your (email|subscription)"
    r"|security (alert|code)|one[- ]time (code|password)|2fa|two[- ]factor"
    r"|build (failed|passed|succeeded)|deployment (failed|succeeded)"
    r"|weekly (digest|summary|report)|daily (digest|summary|report))\b",
    re.I,
)

# Lines a bulk sender puts in the body. Checked against the whole body, since
# the unsubscribe footer is at the bottom.
_AUTOMATED_BODY = re.compile(
    r"(unsubscribe|manage your (email )?preferences|you (are )?receiv(ed|ing) this"
    r"|this is an automated message|do not reply to this (email|message)"
    r"|view this email in your browser|update your notification settings)",
    re.I,
)

# A calendar invite or a message from ourselves is not correspondence to answer.
_SELF_SENT_SUBJECT = re.compile(r"^(accepted|declined|tentative):", re.I)


def _local_part(address: str) -> str:
    local = (address or "").strip().lower().split("@")[0]
    # Strip the +tag, or "noreply+123@" walks straight through.
    return local.split("+")[0]


def sender_looks_automated(
    *, from_address: str, subject: str = "", body: str = ""
) -> tuple[bool, str]:
    """Return (is_automated, why). ``why`` is for the log, not the user.

    Two independent signals are required from the body alone -- an
    "unsubscribe" link appears in plenty of mail a person actually typed, and
    refusing on that one word would drop real correspondence from anyone whose
    company appends a marketing footer.
    """
    address = (from_address or "").strip().lower()
    if not address or "@" not in address:
        return True, "no usable from-address"

    local = _local_part(address)
    domain = domain_of(address)

    if local.startswith(HARD_REJECT_PREFIXES) or local in HARD_REJECT_PREFIXES:
        return True, f"robot local-part ({local})"
    if local.startswith(_BULK_LOCALS) or local in _BULK_LOCALS:
        return True, f"bulk local-part ({local})"
    if any(marker in domain for marker in AUTOMATED_DOMAIN_MARKERS):
        return True, f"bulk sending domain ({domain})"
    if _SELF_SENT_SUBJECT.search(subject or ""):
        return True, "calendar response"
    if _AUTOMATED_SUBJECT.search(subject or ""):
        return True, "automated subject"

    # Body evidence is weaker, so it takes a second, independent hit before it
    # counts against an address that otherwise looks like a person.
    text = body or ""
    hits = len(set(match.group(0).lower() for match in _AUTOMATED_BODY.finditer(text)))
    if hits >= 2:
        return True, "bulk-mail footer"

    return False, ""
