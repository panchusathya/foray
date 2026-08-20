"""Latency benchmark for the search funnel. Reports p50/p95 over 20 varied
queries with a warm parse cache, plus per-stage timings, for both
``FORAY_RETRIEVAL_ARMS`` settings (hybrid vs legacy score arm).
"""

from __future__ import annotations

import asyncio
import statistics
import time

from foray.api.search import run_search
from foray.config import settings
from foray.db import close_pool, get_pool

QUERIES = [
    "founding ML engineer, shipped training infrastructure",
    "senior backend engineer in Go",
    "infrastructure platform engineer kubernetes",
    "site reliability engineer with prometheus",
    "data engineer spark airflow",
    "database storage engineer rocksdb",
    "ML research engineer transformers",
    "gpu systems engineer cuda kernels",
    "frontend specialist react",
    "mobile engineer swift ios",
    "security engineer fuzzing",
    "full-stack product engineer",
    "ml platform mlops engineer",
    "applied ml engineer rag",
    "senior backend rust distributed systems",
    "founding ml engineer in San Francisco 3-8 years",
    "data engineer with dbt and snowflake",
    "infrastructure engineer terraform envoy",
    "sre with opentelemetry and slo",
    "backend engineer python 5+ years",
]

STAGES = (
    "parse",
    "embed",
    "gucs",
    "count",
    "vector",
    "lexical",
    "score",
    "enrich",
    "rank",
    "hydrate",
)


async def _run_suite(conn, label: str) -> None:
    for q in QUERIES:
        await run_search(conn, q, 40)

    latencies: list[float] = []
    stage_samples: dict[str, list[float]] = {name: [] for name in STAGES}
    for q in QUERIES:
        t0 = time.perf_counter()
        result = await run_search(conn, q, 40)
        latencies.append((time.perf_counter() - t0) * 1000)
        timings = (result.diagnostics or {}).get("timings_ms") or {}
        for name in STAGES:
            if name in timings:
                stage_samples[name].append(float(timings[name]))

    t0 = time.perf_counter()
    result = await run_search(conn, QUERIES[0], 40)
    first_byte_ms = (time.perf_counter() - t0) * 1000

    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    print(f"\n=== arms={label} ===")
    print(f"queries      : {len(latencies)}")
    print(f"p50 latency  : {p50:.1f} ms  (target < 800)")
    print(f"p95 latency  : {p95:.1f} ms  (target < 2000)")
    print(f"max latency  : {max(latencies):.1f} ms")
    print(f"first byte   : {first_byte_ms:.1f} ms  (target < 1000), returned={result.returned}")
    print("stage p50 ms :")
    for name in STAGES:
        samples = stage_samples[name]
        if not samples:
            print(f"  {name:10s}  n/a")
            continue
        print(f"  {name:10s}  {statistics.median(samples):6.1f}")


async def main() -> None:
    original = settings.retrieval_arms
    pool = await get_pool()
    async with pool.acquire() as conn:
        settings.retrieval_arms = "vector,lexical"
        await _run_suite(conn, "vector,lexical")
        settings.retrieval_arms = "score"
        await _run_suite(conn, "score")
    settings.retrieval_arms = original
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
