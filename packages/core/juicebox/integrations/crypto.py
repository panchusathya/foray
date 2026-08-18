"""Encrypt provider tokens at rest.

OAuth refresh tokens grant standing access to a real mailbox, so they never
sit in the database in plaintext. The Fernet key is derived from SECRET_KEY —
rotating SECRET_KEY invalidates stored tokens and users must reconnect.
"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


def _fernet() -> Fernet:
    secret = get_settings().secret_key.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(b"juicebox.tokens." + secret).digest())
    return Fernet(key)


def encrypt_json(data: dict) -> str:
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(payload).decode("ascii")


def decrypt_json(blob: str) -> dict:
    try:
        payload = _fernet().decrypt(blob.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise ValueError("token blob is invalid or was encrypted with a different key") from exc
    return json.loads(payload)
