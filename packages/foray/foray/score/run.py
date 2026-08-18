"""Score gated candidates against archetypes.

Default path: the Anthropic Batch API (50% off), rubric in a cached system prefix,
candidate blob in the user turn, forced JSON output. custom_id is
``{candidate_id}:{archetype_id}`` so results map back statelessly.

Modes:
  --dry-run     print request count + projected $ and exit (no spend)
  --sample N    score N candidates synchronously and print them (no DB write)
  --offline     score with the spend-free heuristic (explicit; model=offline-heuristic)
  (default)     submit a Batch job, poll, parse, upsert into archetype_scores
  --stale-only  only rescore rows whose rubric_hash no longer matches
"""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

from ..config import settings
from ..db import close_pool, get_pool
from ..log import get_logger
from .archetypes import Archetype, load_archetypes, sync_archetypes
from .common import (
    PER_CALL_USD,
    build_user_content,
    candidates_to_score,
    heuristic_result,
    parse_scoring_json,
)

log = get_logger("score.run")

UPSERT = """
insert into archetype_scores
    (candidate_id, archetype_id, score, highlights, rationale, flags, rubric_hash, model, scored_at)
values ($1, $2, $3, $4::jsonb, $5, $6::jsonb, $7, $8, now())
on conflict (candidate_id, archetype_id) do update set
    score = excluded.score,
    highlights = excluded.highlights,
    rationale = excluded.rationale,
    flags = excluded.flags,
    rubric_hash = excluded.rubric_hash,
    model = excluded.model,
    scored_at = now()
"""


async def _upsert(conn, candidate_id: int, arch: Archetype, parsed: dict[str, Any], model: str) -> None:
    import json

    await conn.execute(
        UPSERT,
        candidate_id,
        arch.id,
        parsed["score"],
        json.dumps(parsed["highlights"]),
        parsed["rationale"],
        json.dumps(parsed["flags"]),
        arch.rubric_hash,
        model,
    )


async def _record_error(conn, candidate_id: int, arch_id: str, custom_id: str, raw: str, err: str) -> None:
    await conn.execute(
        "insert into scoring_errors (candidate_id, archetype_id, custom_id, raw_response, error) "
        "values ($1, $2, $3, $4, $5)",
        candidate_id,
        arch_id,
        custom_id,
        raw[:2000],
        err[:500],
    )


# ------------------------------------------------------------------ offline --


async def run_offline(conn, arch: Archetype, rows: list[Any]) -> int:
    for row in rows:
        parsed = heuristic_result(row, arch)
        await _upsert(conn, row["id"], arch, parsed, "offline-heuristic")
    return len(rows)


# -------------------------------------------------------------------- batch --


def _build_request(arch: Archetype, row: Any) -> dict[str, Any]:
    return {
        "custom_id": f"{row['id']}:{arch.id}",
        "params": {
            "model": settings.scoring_model,
            "max_tokens": 400,
            "system": [
                {"type": "text", "text": arch.rubric, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": build_user_content(row)}],
        },
    }


async def run_batch(conn, arch: Archetype, rows: list[Any], poll_interval: float) -> int:
    from anthropic import Anthropic

    client = Anthropic(api_key=settings.anthropic_api_key)
    requests = [_build_request(arch, row) for row in rows]
    row_by_id = {str(row["id"]): row for row in rows}

    batch = client.messages.batches.create(requests=requests)
    log.info("batch.created", batch_id=batch.id, requests=len(requests))
    while True:
        current = client.messages.batches.retrieve(batch.id)
        if current.processing_status == "ended":
            break
        log.info("batch.polling", batch_id=batch.id, status=current.processing_status)
        await asyncio.sleep(poll_interval)

    ok = 0
    failures = 0
    for result in client.messages.batches.results(batch.id):
        custom_id = result.custom_id
        candidate_id_str, _arch_id = custom_id.split(":", 1)
        row = row_by_id.get(candidate_id_str)
        res = result.result
        if res.type != "succeeded":
            failures += 1
            await _record_error(conn, int(candidate_id_str), arch.id, custom_id, "", res.type)
            continue
        text = res.message.content[0].text if res.message.content else ""
        try:
            parsed = parse_scoring_json(text)
            await _upsert(conn, int(candidate_id_str), arch, parsed, settings.scoring_model)
            ok += 1
        except Exception as exc:  # noqa: BLE001 - a bad parse must not kill the batch
            failures += 1
            await _record_error(conn, int(candidate_id_str), arch.id, custom_id, text, str(exc))
    log.info("batch.done", batch_id=batch.id, ok=ok, failures=failures)
    return ok


# ------------------------------------------------------------------- sample --


def run_sample(arch: Archetype, rows: list[Any]) -> None:
    from anthropic import Anthropic

    client = Anthropic(api_key=settings.anthropic_api_key)
    for row in rows:
        msg = client.messages.create(
            model=settings.scoring_model,
            max_tokens=400,
            system=[{"type": "text", "text": arch.rubric, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": build_user_content(row)}],
        )
        text = msg.content[0].text if msg.content else ""
        try:
            parsed = parse_scoring_json(text)
            print(f"\n{row['login']}  score={parsed['score']}  flags={parsed['flags']}")
            for h in parsed["highlights"]:
                print(f"    - {h}")
            print(f"    rationale: {parsed['rationale']}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n{row['login']}  PARSE FAILED: {exc}\n  raw: {text[:200]}")


# --------------------------------------------------------------------- main --


async def amain(args: argparse.Namespace) -> int:
    archetypes = load_archetypes()
    targets = args.archetype.split(",") if args.archetype else list(archetypes)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await sync_archetypes(conn, archetypes)

        plan: list[tuple[Archetype, list[Any]]] = []
        for aid in targets:
            arch = archetypes.get(aid)
            if arch is None:
                raise SystemExit(f"unknown archetype: {aid}")
            limit = args.sample if args.sample else args.limit
            rows = await candidates_to_score(conn, arch, stale_only=args.stale_only, limit=limit)
            plan.append((arch, rows))

        total_calls = sum(len(rows) for _a, rows in plan)
        projected = total_calls * PER_CALL_USD
        print(f"to score: {total_calls} candidate-archetype calls | projected ${projected:.2f}")
        for arch, rows in plan:
            print(f"  {arch.id:22s} {len(rows):6d}")

        if args.dry_run:
            return 0

        if not args.offline and not args.sample:
            settings.require("anthropic_api_key")
            if projected > settings.spend_confirm_threshold_usd and not args.yes:
                raise SystemExit(
                    f"projected ${projected:.2f} exceeds ${settings.spend_confirm_threshold_usd:.0f}; "
                    f"re-run with --yes to confirm."
                )

        scored = 0
        for arch, rows in plan:
            if not rows:
                continue
            if args.sample:
                settings.require("anthropic_api_key")
                run_sample(arch, rows)
            elif args.offline:
                scored += await run_offline(conn, arch, rows)
            else:
                scored += await run_batch(conn, arch, rows, args.poll_interval)

        if not args.sample:
            print(f"scored {scored} candidate-archetype pairs")
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Score gated candidates against archetypes.")
    ap.add_argument("--archetype", help="comma-separated ids (default: all)")
    ap.add_argument("--limit", type=int, default=0, help="cap candidates per archetype")
    ap.add_argument("--dry-run", action="store_true", help="print request count + cost, then exit")
    ap.add_argument("--yes", action="store_true", help="confirm spend above the threshold")
    ap.add_argument("--sample", type=int, default=0, help="score N synchronously and print (no DB write)")
    ap.add_argument("--offline", action="store_true", help="spend-free heuristic scorer (explicit)")
    ap.add_argument("--stale-only", action="store_true", help="only rescore rubric_hash mismatches")
    ap.add_argument("--poll-interval", type=float, default=15.0, help="batch poll seconds")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
