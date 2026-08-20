"""Password hashing, API keys and compact HS256 tokens — stdlib only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from .config import get_settings

_PBKDF2_ROUNDS = 240_000


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, hash_hex = encoded.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# --------------------------------------------------------------------------
# api keys
# --------------------------------------------------------------------------


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, sha256 hash, display prefix)."""
    raw = "jbx_" + secrets.token_urlsafe(32)
    return raw, hash_api_key(raw), raw[:12]


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# tokens (compact JWT, HS256)
# --------------------------------------------------------------------------


class TokenError(Exception):
    pass


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(message: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def create_access_token(
    *,
    subject: str,
    org_id: str,
    role: str,
    ttl_seconds: int | None = None,
    extra: dict | None = None,
) -> str:
    """Sign a compact HS256 token.

    ``extra`` carries non-identity claims for single-purpose tokens — the OAuth
    connect state uses it to remember which mail provider the round trip is
    for. It cannot overwrite the reserved claims below.
    """
    settings = get_settings()
    now = int(time.time())
    payload = dict(extra or {})
    payload.update({
        "sub": subject,
        "org": org_id,
        "role": role,
        "iat": now,
        "exp": now + (ttl_seconds or settings.access_token_ttl_seconds),
    })
    header = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode("ascii")
    signature = _b64e(_sign(signing_input, settings.secret_key))
    return f"{header}.{body}.{signature}"


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    try:
        header_b64, body_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise TokenError("malformed token") from exc

    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    expected = _sign(signing_input, settings.secret_key)
    try:
        provided = _b64d(signature_b64)
    except Exception as exc:
        raise TokenError("malformed signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise TokenError("bad signature")

    try:
        payload = json.loads(_b64d(body_b64))
    except Exception as exc:
        raise TokenError("malformed payload") from exc

    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenError("token expired")
    return payload


def signed_tracking_token(message_id: str) -> str:
    """Short HMAC so tracking pixels can't be forged for arbitrary message ids."""
    secret = get_settings().secret_key
    return _b64e(_sign(message_id.encode("utf-8"), secret))[:16]


def verify_tracking_token(message_id: str, token: str) -> bool:
    return hmac.compare_digest(signed_tracking_token(message_id), token)
