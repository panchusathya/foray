"""Import a Bright Data LinkedIn export into the candidate pool.

    python -m foray.ingest.linkedin_file --path data/in/snap.json [--limit N]
        [--batch 500] [--resume] [--dry-run] [--geo-filter/--no-geo-filter]

Writes candidates (source='linkedin', login='li:'+slug), raw_profiles
(source='linkedin', the whole record — the zero-API re-derive guarantee), and
any business emails found in free text (source='linkedin', confidence 80).
Deliberately writes NO candidate_signals: derive stays the single signals
writer, exactly as with GitHub.

Idempotent (`on conflict (login)`) and resumable: a checkpoint row stores the
byte offset per file fingerprint, and because writes are idempotent an
over-read after resume is harmless. Peak memory is one batch of records.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from ..db import get_pool
from ..enrich.emails import UPSERT_EMAIL
from ..enrich.employer import is_work_domain
from ..log import Heartbeat, get_logger
from .linkedin_map import is_target_geo, to_import_row
from .stream import file_key, stream_records


def _jsonb_safe(record: dict) -> str:
    """Serialise for a jsonb column. Postgres rejects \\u0000 anywhere in
    jsonb text, and a handful of scraped profiles carry embedded NULs — one
    poison record otherwise aborts its whole 500-row batch."""
    return (
        json.dumps(record, ensure_ascii=False)
        .replace("\\u0000", "")
        .replace("\x00", "")
    )

log = get_logger("ingest.linkedin")

# Not nightly's lock (0x0F0A): the import may run alongside a nightly, but two
# imports of the same corpus must never interleave.
IMPORT_LOCK_KEY = 0x11D1


_UPSERT_CANDIDATE = """
insert into candidates
    (login, name, location_raw, linkedin_url, linkedin_slug, linkedin_source,
     linkedin_checked_at, source, discovery_shard)
values ($1, $2, $3, $4, $5, 'bright_data', now(), 'linkedin', $6)
on conflict (login) do update set
    name          = excluded.name,
    location_raw  = excluded.location_raw,
    linkedin_url  = excluded.linkedin_url,
    linkedin_slug = excluded.linkedin_slug
returning id
"""

_UPSERT_RAW = """
insert into raw_profiles (candidate_id, source, login, data, fetched_at)
values ($1, 'linkedin', $2, $3::jsonb, now())
on conflict (candidate_id, source) do update set
    data = excluded.data, login = excluded.login, fetched_at = now()
"""


async def import_file(
    *,
    path: Path,
    limit: int = 0,
    batch_size: int = 500,
    resume: bool = False,
    dry_run: bool = False,
    geo_filter: bool = True,
) -> dict:
    pool = await get_pool()
    key = file_key(path)
    shard_tag = f"brightdata:{key[:12]}"
    stats = {"seen": 0, "upserted": 0, "skipped": 0, "emails": 0, "geo_skipped": 0}

    async with pool.acquire() as conn:
        locked = await conn.fetchval("select pg_try_advisory_lock($1)", IMPORT_LOCK_KEY)
        if not locked:
            log.info("import.skipped", reason="another import holds the lock")
            print("another import is running; refusing to interleave")
            return stats
        try:
            start_offset = 0
            if resume:
                row = await conn.fetchrow(
                    "select byte_offset, records_done from import_checkpoints where file_key = $1",
                    key,
                )
                if row:
                    start_offset = row["byte_offset"]
                    log.info(
                        "import.resume", offset=start_offset, records_done=row["records_done"]
                    )

            batch: list[tuple[dict, int]] = []

            async def flush(offset: int) -> None:
                if dry_run or not batch:
                    batch.clear()
                    return
                async with conn.transaction():
                    for record, _ in batch:
                        row = to_import_row(record)
                        if row is None:
                            continue
                        candidate_id = await conn.fetchval(
                            _UPSERT_CANDIDATE,
                            row.login,
                            row.name,
                            row.location_raw,
                            row.linkedin_url,
                            row.slug,
                            shard_tag,
                        )
                        await conn.execute(_UPSERT_RAW, candidate_id, row.login, _jsonb_safe(record))
                        for email in row.emails:
                            await conn.execute(
                                UPSERT_EMAIL,
                                candidate_id,
                                email,
                                "linkedin",
                                is_work_domain(email),
                                80,
                                False,
                                f"bright_data export {key[:12]}",
                                1,
                            )
                            stats["emails"] += 1
                        stats["upserted"] += 1
                    await conn.execute(
                        """
                        insert into import_checkpoints
                            (file_key, path, records_done, byte_offset, rows_upserted)
                        values ($1, $2, $3, $4, $5)
                        on conflict (file_key) do update set
                            records_done  = excluded.records_done,
                            byte_offset   = excluded.byte_offset,
                            rows_upserted = excluded.rows_upserted,
                            updated_at    = now()
                        """,
                        key,
                        str(path),
                        stats["seen"],
                        offset,
                        stats["upserted"],
                    )
                batch.clear()

            last_offset = start_offset
            with Heartbeat(log, lambda: dict(stats)):
                for record, offset in stream_records(path, start_offset=start_offset):
                    stats["seen"] += 1
                    last_offset = offset
                    if geo_filter and not is_target_geo(record):
                        stats["geo_skipped"] += 1
                    elif to_import_row(record) is None:
                        stats["skipped"] += 1
                    else:
                        batch.append((record, offset))
                    if len(batch) >= batch_size:
                        await flush(offset)
                    if limit and stats["seen"] >= limit:
                        break
                await flush(last_offset)

            if not dry_run and not limit:
                await conn.execute(
                    "update import_checkpoints set finished_at = now() where file_key = $1", key
                )
        finally:
            await conn.execute("select pg_advisory_unlock($1)", IMPORT_LOCK_KEY)

    log.info("import.done", **stats)
    print(
        f"seen {stats['seen']:,} | upserted {stats['upserted']:,} | "
        f"geo-skipped {stats['geo_skipped']:,} | unusable {stats['skipped']:,} | "
        f"emails {stats['emails']:,}" + (" [dry-run]" if dry_run else "")
    )
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a Bright Data LinkedIn export.")
    ap.add_argument("--path", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=0, help="stop after N records (testing)")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--resume", action="store_true", help="continue from the checkpoint")
    ap.add_argument("--dry-run", action="store_true", help="parse and count, write nothing")
    ap.add_argument(
        "--no-geo-filter",
        dest="geo_filter",
        action="store_false",
        help="import non-US rows too (default filters to country_code US)",
    )
    args = ap.parse_args()
    if not args.path.exists():
        print(f"no such file: {args.path}")
        return 2

    started = datetime.now(UTC)
    asyncio.run(
        import_file(
            path=args.path,
            limit=args.limit,
            batch_size=args.batch,
            resume=args.resume,
            dry_run=args.dry_run,
            geo_filter=args.geo_filter,
        )
    )
    print(f"elapsed: {(datetime.now(UTC) - started).total_seconds():.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
