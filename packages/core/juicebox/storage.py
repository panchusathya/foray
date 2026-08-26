"""Where candidate document bytes live.

The repo's first blob store, and deliberately the smallest one that is
correct: DB rows own the metadata (``CandidateDocument``), the filesystem owns
the bytes, and the path is derived from ids rather than from anything a client
sent. CANDIDATE_AGENT_PLAN §7 called the storage decision the thing that
stalls resume ingest — this resolves it as "local filesystem behind the API",
one config value (`JUICEBOX_FILE_STORAGE_DIR`) away from any future move,
because ``storage_path`` is stored on the row and a move is a data migration.

Writes are atomic (tmp file + ``os.replace``): a crash mid-write leaves either
the old bytes or no file, never a truncated PDF that parses as garbage.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

from .config import get_settings

logger = logging.getLogger(__name__)


def storage_root() -> Path:
    return Path(get_settings().file_storage_dir)


def document_relpath(*, org_id: str, candidate_id: str, document_id: str, suffix: str = ".pdf") -> str:
    """``{org}/{candidate}/{doc}.pdf`` — ids only, nothing client-supplied.

    A path built from a filename would be a traversal bug waiting for a
    reviewer to miss it; ids are ours and shell-safe by construction.
    """
    return f"{org_id}/{candidate_id}/{document_id}{suffix}"


def pending_attachment_relpath(
    *, org_id: str, thread_id: str, message_id: str, suffix: str = ".pdf"
) -> str:
    """``{org}/threads/{thread}/{message}.pdf`` — a resume held for a thread
    that cannot own a document yet (no candidate to attach it to). Moved to a
    ``document_relpath`` home when the identity arrives, deleted after."""
    return f"{org_id}/threads/{thread_id}/{message_id}{suffix}"


#: Only these characters survive into an artifact label. The label names a
#: step in a browser run ("before-submit"), so it is ours, but a path segment
#: built from anything but ids gets checked rather than trusted.
_LABEL_SAFE = re.compile(r"[^a-z0-9._-]+")


def artifact_relpath(*, org_id: str, application_id: str, label: str, suffix: str = ".png") -> str:
    """``{org}/applications/{app}/{label}.png`` for submission evidence.

    Kept under the application rather than the candidate because that is how it
    is read back: the question an artifact answers is always "what did we send
    to this employer", never "what files does this person have".
    """
    cleaned = _LABEL_SAFE.sub("-", (label or "step").strip().lower()).strip("-") or "step"
    base = f"{org_id}/applications/{application_id}/{cleaned}"
    relpath = f"{base}{suffix}"
    # A retried attempt captures the same step again; keep both.
    counter = 2
    while (storage_root() / relpath).exists():
        relpath = f"{base}-{counter}{suffix}"
        counter += 1
    return relpath


def document_abspath(relpath: str) -> Path:
    root = storage_root()
    resolved = (root / relpath).resolve()
    # Belt and braces: the relpath is server-built, but a stored row edited by
    # hand must still not read outside the store.
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"storage path {relpath!r} escapes the document store")
    return resolved


def save_bytes(relpath: str, data: bytes) -> tuple[int, str]:
    """Write atomically. Returns ``(size_bytes, sha256)`` for the row."""
    target = document_abspath(relpath)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except PermissionError as exc:
        # This surfaces verbatim on the application page as "Package build
        # failed", where a bare errno tells a recruiter nothing. In production
        # it has exactly one cause: a named volume the daemon created as root
        # under a container that runs as uid 10001.
        raise PermissionError(
            f"cannot write to the document store at {storage_root()} — "
            "the directory is not writable by the app user "
            "(see deploy/README.md, 'filedata needs a one-time chown')"
        ) from exc
    return len(data), hashlib.sha256(data).hexdigest()


def read_bytes(relpath: str) -> bytes:
    return document_abspath(relpath).read_bytes()


def delete_file(relpath: str) -> None:
    """Best-effort: the row's deletion drives this, a missing file is fine."""
    try:
        document_abspath(relpath).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not delete document file %s: %s", relpath, exc)
