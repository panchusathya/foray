#!/usr/bin/env python3
"""Assert every Z-Fellows answer fits its character limit.

Z-Fellows caps most answers at 500 characters and two at 250. Blowing a hard limit
at submission time is the most likely concrete failure in this pack, and the cheapest
to rule out. Run it after every edit to ``forms/z-fellows.md``.

Expected format in that file -- the limit lives in the heading::

    ## Z1 - What is the project you are working on? (limit: 500)

    Answer text, everything up to the next ## heading.

Exit status is 1 if any answer is over, or if any answer is missing, so this can gate
a commit.

    python applications/check_lengths.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FORM = Path(__file__).parent / "forms" / "z-fellows.md"

# "## Z1 - ... (limit: 500)" -- the id, the question, and the cap.
HEADING = re.compile(r"^##\s+(Z\d+)\s*[-—]\s*(.*?)\s*\(limit:\s*(\d+)\)\s*$", re.M)

PLACEHOLDER = re.compile(r"\bTK\b|\bTODO\b|\bGAP\b|\[\.\.\.\]")


def answers(text: str) -> list[tuple[str, str, int, str]]:
    """(id, question, limit, answer) for each heading, in document order."""
    found = list(HEADING.finditer(text))
    out = []
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(text)
        body = text[m.end() : end].strip()
        out.append((m.group(1), m.group(2), int(m.group(3)), body))
    return out


def main() -> int:
    if not FORM.exists():
        print(f"not written yet: {FORM}")
        return 1

    rows = answers(FORM.read_text(encoding="utf-8"))
    if not rows:
        print(f"no answers found in {FORM} -- check the heading format")
        return 1

    failed = False
    print(f"{'id':<5} {'chars':>7} {'limit':>7} {'headroom':>9}  status")
    print("-" * 52)

    for aid, question, limit, body in rows:
        n = len(body)
        headroom = limit - n
        if not body:
            status, failed = "EMPTY", True
        elif PLACEHOLDER.search(body):
            status, failed = "PLACEHOLDER", True
        elif n > limit:
            status, failed = f"OVER by {n - limit}", True
        elif headroom > limit * 0.4:
            # Well under a hard cap usually means the answer was left thin, not that
            # it is admirably concise. Worth a look, but not a failure.
            status = "short?"
        else:
            status = "ok"
        print(f"{aid:<5} {n:>7} {limit:>7} {headroom:>9}  {status}")

    print()
    if failed:
        print("FAIL -- fix the rows above before submitting.")
        return 1
    print(f"PASS -- {len(rows)} answers, all within limits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
