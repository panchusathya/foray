"""Score gated candidates against archetypes.

Default path: bounded-concurrency chat completions against OpenRouter (any
OpenRouter model slug; FORAY_SCORING_MODEL defaults to a cheap open-weight
model). Rubric in the system turn, candidate blob in the user turn, forced JSON
output parsed defensively. OpenRouter has no batch API, so bulk scoring is a
fan-out of single requests with a semaphore.

Modes:
  --dry-run     print request count + projected $ and exit (no spend)
  --sample N    score N candidates synchronously and print them (no DB write)
  --offline     score with the spend-free heuristic (explicit; model=offline-heuristic)
  (default)     fan out requests, parse, upsert into archetype_scores
  --stale-only  only rescore rows whose rubric_hash no longer matches

Every mode that spends is additionally bounded by FORAY_LINKEDIN_SCORE_CAP, a
run-wide ceiling on LinkedIn-evidence calls (see _linkedin_capped).
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from ..config import settings
from ..db import close_pool, get_pool
from ..llm import AsyncChat, AsyncOpenRouterChat, Chat, OpenRouterChat
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


# --------------------------------------------------------------- openrouter --


async def run_openrouter(
    conn,
    arch: Archetype,
    rows: list[Any],
    concurrency: int,
    chat: AsyncChat | None = None,
) -> int:
    """Fan out completions with a semaphore; DB writes stay on this task.

    Requests run concurrently, but upserts/error rows are applied sequentially
    from the gathered results because asyncpg connections are not task-safe.
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def score_one(client: AsyncChat, row: Any) -> tuple[Any, str, Exception | None]:
        async with semaphore:
            try:
                text = await client.complete(
                    arch.rubric,
                    build_user_content(row),
                    model=settings.scoring_model,
                )
                return row, text, None
            except Exception as exc:  # noqa: BLE001 - recorded per-row, must not kill the run
                return row, "", exc

    if chat is None:
        async with AsyncOpenRouterChat() as client:
            results = await asyncio.gather(*(score_one(client, row) for row in rows))
    else:
        results = await asyncio.gather(*(score_one(chat, row) for row in rows))

    ok = 0
    failures = 0
    for row, text, exc in results:
        custom_id = f"{row['id']}:{arch.id}"
        if exc is not None:
            failures += 1
            await _record_error(conn, row["id"], arch.id, custom_id, "", str(exc))
            continue
        try:
            parsed = parse_scoring_json(text)
            await _upsert(conn, row["id"], arch, parsed, settings.scoring_model)
            ok += 1
        except Exception as exc2:  # noqa: BLE001 - a bad parse must not kill the run
            failures += 1
            await _record_error(conn, row["id"], arch.id, custom_id, text, str(exc2))
    log.info("openrouter.done", archetype=arch.id, ok=ok, failures=failures)
    return ok


# ------------------------------------------------------------------- sample --


def _linkedin_capped(
    plan: list[tuple[Archetype, list[Any]]], cap: int
) -> tuple[list[tuple[Archetype, list[Any]]], int]:
    """Trim LinkedIn-evidence rows to ``cap`` calls across the whole run.

    The cap FORAY_LINKEDIN_SCORE_CAP documents ("the difference between ~$900
    and ~$10") was defined in config.py and read by nothing -- the gates alone
    were holding the line. It is enforced here rather than in
    candidates_to_score because it is a budget for the run, not a predicate on
    a row: it has to be spent across archetypes in one place, and it has to see
    what --limit already produced.

    LinkedIn rows are the ones capped because they are the ones there are
    200,000 of, and their evidence is a profile rather than code -- the least
    marginal value per call in the pool. GitHub-evidence rows are never
    trimmed. Returns (plan, dropped).
    """
    if cap <= 0:
        return plan, 0
    budget = cap
    dropped = 0
    trimmed: list[tuple[Archetype, list[Any]]] = []
    for arch, rows in plan:
        kept = []
        for row in rows:
            if row.get("evidence_kind", "github") == "linkedin":
                if budget <= 0:
                    dropped += 1
                    continue
                budget -= 1
            kept.append(row)
        trimmed.append((arch, kept))
    return trimmed, dropped


def run_sample(arch: Archetype, rows: list[Any], chat: Chat | None = None) -> None:
    completion = OpenRouterChat() if chat is None else chat
    for row in rows:
        text = completion.complete(
            arch.rubric, build_user_content(row), model=settings.scoring_model
        )
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

        # Before --max-calls, because it is a ceiling and not an allocation:
        # trimming first means the proportional split below divides what is
        # actually payable. --offline is exempt; it spends nothing.
        if not args.offline:
            plan, dropped_linkedin = _linkedin_capped(plan, settings.linkedin_score_cap)
            if dropped_linkedin:
                log.info(
                    "score.linkedin_capped",
                    cap=settings.linkedin_score_cap,
                    dropped=dropped_linkedin,
                )
                print(
                    f"linkedin cap {settings.linkedin_score_cap}: "
                    f"{dropped_linkedin} LinkedIn-evidence rows deferred to a later run"
                )

        # --max-calls is a GLOBAL budget across all archetypes (unlike --limit,
        # which is per-archetype and so caps at 15x). Allocate it PROPORTIONALLY
        # so a nightly budget doesn't starve archetypes 4-15 for weeks.
        if args.max_calls and not args.sample:
            nonempty = [(a, r) for a, r in plan if r]
            if nonempty:
                per_arch = max(1, args.max_calls // len(nonempty))
                budget_left = args.max_calls
                trimmed = []
                for a, r in plan:
                    take = min(len(r), per_arch, budget_left)
                    trimmed.append((a, r[:take]))
                    budget_left -= take
                # Hand any leftover budget to archetypes that still have rows.
                if budget_left > 0:
                    for idx, (a, r) in enumerate(plan):
                        extra = min(len(r) - len(trimmed[idx][1]), budget_left)
                        if extra > 0:
                            trimmed[idx] = (a, r[: len(trimmed[idx][1]) + extra])
                            budget_left -= extra
                plan = trimmed

        total_calls = sum(len(rows) for _a, rows in plan)
        projected = total_calls * PER_CALL_USD
        print(
            f"to score: {total_calls} candidate-archetype calls | "
            f"~${projected:.2f} at {settings.scoring_model}"
        )
        for arch, rows in plan:
            print(f"  {arch.id:22s} {len(rows):6d}")

        if args.dry_run:
            return 0

        if not args.offline:
            settings.require("openrouter_api_key")
            if not args.sample and projected > settings.spend_confirm_threshold_usd and not args.yes:
                raise SystemExit(
                    f"projected ${projected:.2f} exceeds ${settings.spend_confirm_threshold_usd:.0f}; "
                    f"re-run with --yes to confirm."
                )

        scored = 0
        for arch, rows in plan:
            if not rows:
                continue
            if args.sample:
                run_sample(arch, rows)
            elif args.offline:
                scored += await run_offline(conn, arch, rows)
            else:
                scored += await run_openrouter(conn, arch, rows, args.concurrency)

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
    ap.add_argument(
        "--max-calls",
        type=int,
        default=0,
        help="global cap on candidate-archetype calls this run (0 = no cap), split across archetypes",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=settings.scoring_concurrency,
        help="parallel OpenRouter requests (default FORAY_SCORING_CONCURRENCY)",
    )
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
