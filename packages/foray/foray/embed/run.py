"""Embed candidate blobs into halfvec(384) and build the HNSW index.

Skips candidates whose blob is unchanged (diffed against candidates.embed_blob).
The HNSW index is created after the bulk load, per pgvector guidance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from ..db import close_pool, get_pool
from ..log import Heartbeat, get_logger
from .blob import build_blob
from .model import EMBEDDING_MODEL_VERSION, embed_documents, to_halfvec_literal

log = get_logger("embed.run")

HNSW_INDEX_SQL = (
    "create index if not exists ix_candidates_embedding "
    "on candidates using hnsw (embedding halfvec_cosine_ops) "
    "with (m = 16, ef_construction = 64)"
)


def _as_dict(value: Any) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _as_list(value: Any) -> list:
    if isinstance(value, str):
        return json.loads(value)
    return value or []


def blob_for_row(row: Any) -> str:
    return build_blob(
        name=row["name"],
        login=row["login"],
        bio=row["bio"],
        primary_language=row["primary_language"],
        language_mix=_as_dict(row["language_mix"]),
        employer=row["employer"],
        title=row["title"],
        top_repos=_as_list(row["top_repos"]),
        topics=list(row["topics"] or []),
    )


async def amain(args: argparse.Namespace) -> int:
    pool = await get_pool()
    embedded = 0
    skipped = 0
    async with pool.acquire() as conn:
        # By default only fetch rows whose blob inputs could have changed since
        # the last embed: a signals re-derive (derived_at) or a profile refetch
        # (fetched_at) after embedded_at, or never embedded. This is a strict
        # SUPERSET of "actually changed" (the Python blob diff below catches the
        # false positives cheaply), and it keeps the nightly run from pulling
        # every candidate+signals into memory at 100k. --all restores the full
        # scan; a change to blob.py itself needs --all --force.
        changed = (
            "where c.embedding is null or c.embedded_at is null or c.embed_blob is null "
            "or s.derived_at > c.embedded_at "
            "or (r.fetched_at is not null and r.fetched_at > c.embedded_at)"
        )
        rows = await conn.fetch(
            f"""
            select c.id, c.login, c.name, c.embed_blob as old_blob,
                   (c.embedding is null) as needs_vector,
                   r.data->>'bio' as bio,
                   s.employer, s.title, s.primary_language, s.language_mix,
                   s.top_repos, s.topics
            from candidates c
            join candidate_signals s on s.candidate_id = c.id
            left join raw_profiles r on r.candidate_id = c.id
            {"" if args.all else changed}
            order by c.id
            """
            + (" limit $1" if args.limit else ""),
            *([args.limit] if args.limit else []),
        )

        pending: list[tuple[int, str]] = []
        for row in rows:
            blob = blob_for_row(row)
            if not args.force and not row["needs_vector"] and blob == (row["old_blob"] or ""):
                skipped += 1
                continue
            pending.append((row["id"], blob))

        log.info("embed.plan", total=len(rows), to_embed=len(pending), skipped=skipped)

        with Heartbeat(log, lambda: {"embedded": embedded, "skipped": skipped}):
            for start in range(0, len(pending), args.batch_size):
                chunk = pending[start : start + args.batch_size]
                vectors = embed_documents([blob for _cid, blob in chunk])
                updates = [
                    (to_halfvec_literal(vec), blob, EMBEDDING_MODEL_VERSION, cid)
                    for (cid, blob), vec in zip(chunk, vectors, strict=True)
                ]
                await conn.executemany(
                    "update candidates set embedding = $1::halfvec, embed_blob = $2, "
                    "embedding_model = $3, embedded_at = now() where id = $4",
                    updates,
                )
                embedded += len(chunk)

        if embedded and not args.skip_index:
            log.info("embed.index.build")
            await conn.execute(HNSW_INDEX_SQL)

    log.info("embed.done", embedded=embedded, skipped=skipped)
    print(f"embedded {embedded} candidates ({skipped} unchanged, skipped)")
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Embed candidate blobs into halfvec(384).")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--all", action="store_true", help="scan every candidate, not just changed")
    ap.add_argument("--force", action="store_true", help="re-embed even if blob unchanged")
    ap.add_argument("--skip-index", action="store_true", help="do not build the HNSW index")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
