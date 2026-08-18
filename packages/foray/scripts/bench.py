"""Latency benchmark for the search funnel. Reports p50/p95 over 20 varied
queries with a warm parse cache, plus time-to-first-SSE-byte.
"""

from __future__ import annotations

import asyncio
import statistics
import time

from foray.api.search import run_search
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


async def main() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Warm the parse cache + embedding model.
        for q in QUERIES:
            await run_search(conn, q, 40)

        latencies = []
        for q in QUERIES:
            t0 = time.perf_counter()
            await run_search(conn, q, 40)
            latencies.append((time.perf_counter() - t0) * 1000)

        # Time to first byte of the streaming path (first candidate ready).
        t0 = time.perf_counter()
        result = await run_search(conn, QUERIES[0], 40)
        first_byte_ms = (time.perf_counter() - t0) * 1000

    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    print(f"queries      : {len(latencies)}")
    print(f"p50 latency  : {p50:.1f} ms  (target < 800)")
    print(f"p95 latency  : {p95:.1f} ms  (target < 2000)")
    print(f"max latency  : {max(latencies):.1f} ms")
    print(f"first byte   : {first_byte_ms:.1f} ms  (target < 1000), returned={result.returned}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
