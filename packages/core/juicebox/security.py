"""Password hashing, API keys and compact HS256 tokens — stdlib only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from .config import get_settings

# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    rounds = get_settings().pbkdf2_rounds
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${dk.hex()}"


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


def signed_intake_token(purpose: str, subject: str) -> str:
    """Purpose-scoped HMAC for an endpoint whose caller has no principal.

    Two of these exist. A *widget* token is minted per org and embedded in the
    marketing site, so the org a chat belongs to is carried by something only
    we can produce -- the pattern the tracking endpoints use, and the reason a
    payload-supplied org is never trusted. A *thread* token is handed to the
    browser when a conversation opens and is what lets it keep posting to a
    conversation nobody is authenticated against.

    ``purpose`` is inside the signature, so a thread token cannot be replayed
    as a widget token to open conversations against an org, and vice versa.
    Not truncated the way ``signed_tracking_token`` is: that one guards a
    counter, these authorise writes.
    """
    secret = get_settings().secret_key
    return _b64e(_sign(f"{purpose}:{subject}".encode(), secret))


def verify_intake_token(purpose: str, subject: str, token: str) -> bool:
    return hmac.compare_digest(signed_intake_token(purpose, subject), token or "")


#: How much of the HMAC goes into an email local part. Long enough that an
#: address cannot be enumerated, short enough that the whole thing still fits
#: the 64-character local-part limit and reads as a machine address rather than
#: a wall of base64.
ALIAS_TOKEN_CHARS = 18


def alias_token(candidate_id: str) -> str:
    """The sub-address token for one candidate's application alias.

    Derived rather than stored so ``anna+<token>@goforay.io`` can be *verified*
    from the address alone -- inbound mail arrives before any lookup, and an
    unsigned local part would let anyone address a candidate's application
    inbox by guessing an id. Purpose-scoped like ``signed_intake_token``, so an
    alias token is not interchangeable with a widget or thread token.

    Lowercased because mail local parts survive case changes in transit, and a
    token that fails to match after a relay lowercased it is unroutable.
    """
    secret = get_settings().secret_key
    signed = _b64e(_sign(f"alias:{candidate_id}".encode(), secret))
    # base64url still contains "-" and "_", both legal in a local part, but keep
    # it alphanumeric so no provider's parser has an opinion.
    cleaned = "".join(ch for ch in signed if ch.isalnum()).lower()
    return cleaned[:ALIAS_TOKEN_CHARS]


def verify_alias_token(candidate_id: str, token: str) -> bool:
    return hmac.compare_digest(alias_token(candidate_id), (token or "").strip().lower())
