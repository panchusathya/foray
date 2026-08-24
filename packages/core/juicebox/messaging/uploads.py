"""The signed resume-upload link the agent texts.

CANDIDATE_AGENT_PLAN §7 decided how a resume arrives: **not as media over the
message channel** — carrier and vendor media limits present as the product
losing documents — but as a link to an upload page, authorised by the same
purpose-scoped HMAC family the chat widget uses. The token is bound to one
thread; knowing a thread id is not enough to push files into somebody's
intake, and an upload token cannot be replayed as a chat token.

Non-expiring by design, like the thread token it mirrors: the person may tap
the link a day later, and the thread's own lifecycle (STOP, completion) is
what ends its usefulness.
"""

from __future__ import annotations

from ..config import get_settings
from ..models import IntakeThread
from ..security import signed_intake_token, verify_intake_token

PURPOSE_UPLOAD = "intake-upload"


def mint_upload_link(thread: IntakeThread) -> str:
    """The absolute URL the agent drops into a message."""
    base = get_settings().base_url.rstrip("/")
    token = signed_intake_token(PURPOSE_UPLOAD, thread.id)
    return f"{base}/v1/intake/uploads/{thread.id}?token={token}"


def verify_upload_token(thread_id: str, token: str) -> bool:
    return verify_intake_token(PURPOSE_UPLOAD, thread_id, token)
