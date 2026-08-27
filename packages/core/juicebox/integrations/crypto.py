"""Encrypt secrets at rest with a small, explicit key ring.

OAuth tokens can be reconnected, but candidate-supplied ATS passwords cannot
be reconstructed.  New blobs carry a version prefix and reads accept the
current key plus configured previous keys.  Callers that own a row may use
``needs_reencrypt`` to lazily rotate it after a successful read.
"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


def _key(secret: str) -> bytes:
    secret = secret.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(b"juicebox.tokens." + secret).digest())
    return key


def _fernets() -> list[Fernet]:
    settings = get_settings()
    return [Fernet(_key(secret)) for secret in (settings.secret_key, *settings.secret_key_previous)]


def encrypt_json(data: dict) -> str:
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return "v1:" + _fernets()[0].encrypt(payload).decode("ascii")


def decrypt_json(blob: str) -> dict:
    raw = (blob or "").removeprefix("v1:")
    for fernet in _fernets():
        try:
            return json.loads(fernet.decrypt(raw.encode("ascii")))
        except (InvalidToken, ValueError, json.JSONDecodeError):
            continue
    raise ValueError("token blob is invalid or was encrypted with a different key")


def needs_reencrypt(blob: str) -> bool:
    """True when a readable blob was encrypted with an old or legacy key."""
    raw = (blob or "").removeprefix("v1:")
    try:
        _fernets()[0].decrypt(raw.encode("ascii"))
        return not (blob or "").startswith("v1:")
    except (InvalidToken, ValueError):
        return True
