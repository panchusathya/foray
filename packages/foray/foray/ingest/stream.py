"""Bounded-memory record streaming for huge JSON exports.

The Bright Data snapshot is a 1.9 GB JSON array on a single line, so neither
``json.load`` (2 GB in RAM) nor line-splitting works. This scans the character
stream tracking brace depth and string state, yielding one decoded object at a
time; peak memory is the largest single record. JSONL files stream too, so one
reader covers both deliveries.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

CHUNK_CHARS = 1 << 22  # 4 MiB of text per read


def stream_records(path: Path, *, start_offset: int = 0) -> Iterator[tuple[dict, int]]:
    """Yield (record, byte_offset_after_record) from a JSON array or JSONL file.

    ``start_offset`` resumes mid-file; scanning re-synchronises on the next
    top-level ``{``, and since every downstream write is idempotent, an
    over-read after a coarse offset is harmless.
    """
    depth = 0
    in_string = False
    escaped = False
    started = False
    buf: list[str] = []

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        if start_offset:
            handle.seek(start_offset)
        while True:
            chunk = handle.read(CHUNK_CHARS)
            if not chunk:
                return
            for ch in chunk:
                if not started:
                    if ch == "{":
                        started, depth, buf = True, 1, ["{"]
                    continue
                buf.append(ch)
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            record = json.loads("".join(buf))
                        except json.JSONDecodeError:
                            record = None  # partial record after a mid-file seek
                        buf, started = [], False
                        if record is not None:
                            # tell() is valid here: text-mode offsets are opaque
                            # but seekable, which is all resume needs.
                            yield record, handle.tell()


def file_key(path: Path) -> str:
    """Fingerprint for the checkpoint row: size + a hash of the first MiB."""
    import hashlib

    digest = hashlib.sha256()
    digest.update(str(path.stat().st_size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(1 << 20))
    return digest.hexdigest()[:32]
