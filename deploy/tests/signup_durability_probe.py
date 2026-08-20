#!/usr/bin/env python3
"""Is a signup durable by the time the client uses its token?

TEMPORARY DIAGNOSTIC. Delete this once the cause is fixed.

The browser suite intermittently fails with `bulk upsert failed (401):
{"detail":"Not authenticated"}` right after a successful signup. CI runs #86,
#88 and #89 showed both `POST /v1/candidates/bulk` AND `POST /v1/auth/login`
returning 401 immediately after signup returned 200 with a token -- so the new
user row is findable neither by id (bulk, via the token subject) nor by email
(login). The row is simply not there yet.

That is consistent with how the app commits: no route in packages/core commits
explicitly, and the single `session.commit()` lives in `get_db()`'s teardown,
which FastAPI runs *after* the response has been sent.

This probe does not merely reproduce the failure -- it discriminates between the
two candidate causes, which need different fixes:

  * the row arrives LATE  -> a commit-visibility race; the write happens, just
                             after the client has already acted on the response.
  * the row NEVER arrives -> the post-response commit is failing outright and
                             the write is lost.

On a 401 it re-polls the same credentials for a few seconds. Recovery means
race; permanent 401 means lost write.

Exits 0 no matter what it finds: this collects evidence, it does not gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PASSWORD = "probe-password-9182"
# Pydantic's EmailStr rejects the special-use TLDs, so use an unroutable
# subdomain of a real one -- same trick as packages/web/e2e/support.js.
EMAIL_DOMAIN = "e2e.example.com"


def _redact(body: object) -> object:
    """Keep bearer tokens out of the CI log."""
    if isinstance(body, dict) and "access_token" in body:
        return {**body, "access_token": "<redacted>"}
    return body


def post(base: str, path: str, payload: dict, token: str | None = None) -> tuple[int, object]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read() or b"{}"
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read() or b"").decode(errors="replace")[:200]
    except Exception as exc:  # noqa: BLE001 - a probe reports every failure mode
        return 0, repr(exc)[:200]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    ap.add_argument("--iterations", type=int, default=40)
    ap.add_argument("--recovery-timeout", type=float, default=4.0)
    args = ap.parse_args()

    counts = {
        "signup_failed": 0,
        "login_401": 0,
        "bulk_401": 0,
        "recovered": 0,
        "never_recovered": 0,
    }
    delays_ms: list[float] = []

    for i in range(args.iterations):
        email = f"probe-{int(time.time() * 1000)}-{i}@{EMAIL_DOMAIN}"
        status, body = post(
            args.base,
            "/v1/auth/signup",
            {"org_name": f"Probe {i}", "email": email, "password": PASSWORD},
        )
        token = body.get("access_token") if isinstance(body, dict) else None
        # 2xx, not == 200: signup answers 201 Created. Checking for 200 made the
        # first run count all 40 successful signups as failures and measure
        # nothing -- which is what the "inconclusive" guard below caught.
        if not (200 <= status < 300) or not token:
            counts["signup_failed"] += 1
            print(f"[{i:>3}] signup -> {status} {_redact(body)}", flush=True)
            continue

        # No pause: this is exactly what the e2e helpers do.
        login_status, _ = post(
            args.base, "/v1/auth/login", {"email": email, "password": PASSWORD}
        )
        bulk_status, _ = post(
            args.base,
            "/v1/candidates/bulk",
            {
                "candidates": [
                    {
                        "source": "probe",
                        "source_id": f"probe-{i}",
                        "full_name": f"Probe {i}",
                        "emails": [email],
                    }
                ]
            },
            token,
        )

        if login_status == 401:
            counts["login_401"] += 1
        if bulk_status == 401:
            counts["bulk_401"] += 1
        if login_status != 401 and bulk_status != 401:
            continue

        print(f"[{i:>3}] REPRODUCED  login={login_status} bulk={bulk_status}", flush=True)

        started = time.monotonic()
        deadline = started + args.recovery_timeout
        while time.monotonic() < deadline:
            time.sleep(0.1)
            again, _ = post(
                args.base, "/v1/auth/login", {"email": email, "password": PASSWORD}
            )
            if again == 200:
                waited = (time.monotonic() - started) * 1000
                delays_ms.append(waited)
                counts["recovered"] += 1
                print(f"[{i:>3}]   -> visible after {waited:.0f} ms", flush=True)
                break
        else:
            counts["never_recovered"] += 1
            print(
                f"[{i:>3}]   -> still 401 after {args.recovery_timeout}s; "
                "the row was never persisted",
                flush=True,
            )

    signups_ok = args.iterations - counts["signup_failed"]
    lines = [
        "## Signup durability probe",
        "",
        f"- iterations: **{args.iterations}** ({signups_ok} signups returned a token)",
        f"- `POST /v1/auth/login` 401 straight after signup: **{counts['login_401']}**",
        f"- `POST /v1/candidates/bulk` 401 straight after signup: **{counts['bulk_401']}**",
        f"- became visible on a later poll (late write): **{counts['recovered']}**",
        f"- never became visible (lost write): **{counts['never_recovered']}**",
    ]
    if delays_ms:
        lines.append(
            f"- visibility delay: min {min(delays_ms):.0f} ms, "
            f"max {max(delays_ms):.0f} ms, mean {sum(delays_ms) / len(delays_ms):.0f} ms"
        )

    reproduced = counts["login_401"] or counts["bulk_401"]
    if signups_ok == 0:
        lines += [
            "",
            "**Inconclusive: no signup succeeded**, so nothing was actually "
            + "measured. Check the stack is up and reachable at the given --base.",
        ]
    elif not reproduced:
        lines += [
            "",
            "**Did not reproduce.** This probe hits an idle stack; the e2e "
            + "failures are load-sensitive, so a clean run here does not clear "
            + "the hypothesis.",
        ]
    elif counts["recovered"] and not counts["never_recovered"]:
        lines += [
            "",
            "**Verdict: commit-visibility race.** The write lands, just after "
            + "the response the client already acted on. Commit before returning.",
        ]
    elif counts["never_recovered"] and not counts["recovered"]:
        lines += [
            "",
            "**Verdict: the write is lost, not late.** The post-response commit "
            + "is failing; find out why before reordering anything.",
        ]
    else:
        lines += ["", "**Verdict: mixed** -- both late and lost writes. Read the log above."]

    report = "\n".join(lines)
    print("\n" + report, flush=True)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
