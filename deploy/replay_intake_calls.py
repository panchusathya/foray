#!/usr/bin/env python3
"""Replay calls Retell already has into our webhook.

Every webhook Retell sent while the signing key was wrong was rejected with a
401, so the calls exist on Retell's side -- transcripts and all -- and nowhere
in our database. Retell has long since stopped retrying. This posts them again.

It is not a migration and it writes nothing directly: each call is re-sent as a
normal ``call_analyzed`` webhook, correctly signed, so it goes through exactly
the path a live call takes -- signature check, org resolution from the dialled
number, ``record_call_event``, and the extraction job. Anything wrong with that
path fails here the same way it fails in production, which is the point.

Safe to re-run. ``record_call_event`` is idempotent on
``(provider, provider_call_id)``, so a second pass updates rather than
duplicates.

Usage, from the repo root:

    python deploy/replay_intake_calls.py --base-url https://app.goforay.io --dry-run
    python deploy/replay_intake_calls.py --base-url https://app.goforay.io
    python deploy/replay_intake_calls.py --base-url https://app.goforay.io --call-id call_abc

``--dry-run`` lists what would be sent and posts nothing. Run it first: it also
tells you which numbers the calls came in on, which is what has to be registered
in ``intake_numbers`` for the replay to be accepted.

The key comes from ``RETELL_API_KEY`` in the environment or the repo ``.env``,
and must be the same key the target server has -- that is the whole bug this
exists to recover from. Check with ``deploy/diagnose_intake.sh`` first.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
RETELL_API_BASE = "https://api.retellai.com"


def env_value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value and ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                break
    return value


def retell(path: str, body: dict | None = None) -> object:
    key = env_value("RETELL_API_KEY")
    if not key:
        raise SystemExit("No RETELL_API_KEY in the environment or .env")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{RETELL_API_BASE}{path}",
        data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def sign(raw: bytes, key: str) -> str:
    stamp = str(int(time.time() * 1000))
    digest = hmac.new(key.encode("utf-8"), raw + stamp.encode("utf-8"), hashlib.sha256)
    return f"v={stamp},d={digest.hexdigest()}"


def post_webhook(base_url: str, call: dict, key: str) -> tuple[int, str]:
    # `call_analyzed` rather than `call_ended`: it is the event that carries the
    # post-call analysis, so consent_recording and sms_opt_in land too. Our
    # handler treats both as terminal and queues extraction either way.
    raw = json.dumps({"event": "call_analyzed", "call": call}).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/intake/webhook",
        data=raw,
        headers={"Content-Type": "application/json", "X-Retell-Signature": sign(raw, key)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode()[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:400]
    except urllib.error.URLError as exc:
        return 0, f"unreachable: {exc.reason}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="e.g. https://app.goforay.io")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--call-id", default="", help="replay one call instead of all")
    parser.add_argument("--dry-run", action="store_true", help="list, post nothing")
    args = parser.parse_args()

    key = env_value("RETELL_API_KEY")
    if not key:
        raise SystemExit("No RETELL_API_KEY in the environment or .env")

    calls = retell("/v2/list-calls", {"limit": args.limit})
    if not isinstance(calls, list):
        raise SystemExit(f"unexpected list-calls response: {type(calls).__name__}")
    if args.call_id:
        calls = [c for c in calls if c.get("call_id") == args.call_id]
        if not calls:
            raise SystemExit(f"no such call on Retell: {args.call_id}")

    # Oldest first, so the stored history reads in the order it happened.
    calls.sort(key=lambda c: c.get("start_timestamp") or 0)

    print(f"{len(calls)} call(s) on Retell\n")
    numbers: set[str] = set()
    sent = failed = skipped = 0
    for call in calls:
        cid = call.get("call_id", "")
        direction = call.get("direction", "?")
        # our_number: what the org is resolved from. Outbound dials out, so it
        # is from_number; inbound arrives, so it is to_number.
        ours = call.get("from_number") if direction == "outbound" else call.get("to_number")
        theirs = call.get("to_number") if direction == "outbound" else call.get("from_number")
        tlen = len(call.get("transcript") or "")
        if ours:
            numbers.add(ours)

        if call.get("call_status") not in {"ended", "error"}:
            print(f"  skip  {cid:36} status={call.get('call_status')}")
            skipped += 1
            continue

        # Not truncated: --call-id matches exactly, and this listing is where
        # you copy an id from. A shortened id is one that cannot be pasted back
        # into the tool that printed it.
        line = f"  {cid:36} {direction:9} ours={ours or '-':14} theirs={theirs or '-':14} tscript={tlen:5}"
        if args.dry_run:
            print(f"  plan{line}")
            continue

        status, detail = post_webhook(args.base_url, call, key)
        ok = 200 <= status < 300
        sent += 1 if ok else 0
        failed += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'}{line} -> {status} {detail if not ok else ''}".rstrip())

    print()
    if args.dry_run:
        print("Dry run. Nothing was posted.")
    else:
        print(f"sent={sent} failed={failed} skipped={skipped}")
    if numbers:
        print("\nEvery number below must exist in intake_numbers (active) or the")
        print("webhook answers 404 and the call is refused:")
        for number in sorted(numbers):
            print(f"  {number}    juicebox intake-number {number} --org <org-id>")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
