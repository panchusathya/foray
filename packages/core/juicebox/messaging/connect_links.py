"""The signed connect-your-apps link the agent texts.

Same family as the upload link (``uploads.py``): purpose-scoped HMAC bound to
one thread, non-expiring because the person may tap it a day later, and the
purpose is inside the signature so a connect token cannot be replayed as an
upload or chat token. The page it opens is side-effect-free on GET — iMessage
fetches links to render previews, and a preview must not mint state or write
anything.
"""

from __future__ import annotations

from ..config import get_settings
from ..models import IntakeThread
from ..security import signed_intake_token, verify_intake_token

PURPOSE_CONNECT = "intake-connect"


def mint_connect_link(thread: IntakeThread) -> str:
    """The absolute URL the agent drops into a message."""
    base = get_settings().base_url.rstrip("/")
    token = signed_intake_token(PURPOSE_CONNECT, thread.id)
    return f"{base}/v1/connect/{thread.id}?token={token}"


def verify_connect_token(thread_id: str, token: str) -> bool:
    return verify_intake_token(PURPOSE_CONNECT, thread_id, token)
