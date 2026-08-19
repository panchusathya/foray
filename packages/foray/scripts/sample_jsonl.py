"""Cut a deterministic sample of a huge JSON-array/JSONL export to JSONL.

    python scripts/sample_jsonl.py --path data/snap.json --count 2000 --out sample.jsonl

Deterministic by record index (every Nth record), so dev, staging and prod all
work on reproducible slices of the same file rather than three different
datasets. Streaming: never holds more than one record.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foray.ingest.stream import stream_records


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", type=Path, required=True)
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--total",
        type=int,
        default=200_000,
        help="approximate record count of the source (sets the sampling stride)",
    )
    args = ap.parse_args()

    stride = max(1, args.total // args.count)
    written = 0
    with args.out.open("w", encoding="utf-8") as out:
        for index, (record, _offset) in enumerate(stream_records(args.path)):
            if index % stride:
                continue
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
            if written >= args.count:
                break
    print(f"wrote {written:,} records (every {stride}th) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
