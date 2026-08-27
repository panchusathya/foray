#!/usr/bin/env bash
# One-paste intake diagnosis, to be run ON THE VM from /opt/juicebox.
#
# Read-only. Prints no secrets: the Retell key is reported as a length and a
# sha256 prefix so it can be compared against the workspace key without either
# side pasting it. Everything here answers a question that cannot be answered
# from outside the box.
#
# Optional argument: a phone number in E.164. Passing one adds section 12,
# which answers "why was this caller not recognised?" end to end. Without it
# every other section still runs.
#
#     ./deploy/diagnose_intake.sh
#     ./deploy/diagnose_intake.sh +12134317465
set -uo pipefail
C="docker compose -f docker-compose.prod.yml"
CALLER="${1:-}"

echo "===== 1. Retell key fingerprint (compare, do not paste the key) ====="
$C exec -T api python - <<'PY'
import hashlib, os
k = os.environ.get("RETELL_API_KEY", "")
if not k:
    print("  RETELL_API_KEY: NOT SET  <-- verify() refuses every webhook by design")
else:
    print(f"  length      : {len(k)}")
    print(f"  sha256[:16] : {hashlib.sha256(k.encode()).hexdigest()[:16]}")
    print(f"  prefix/sfx  : {k[:5]}...{k[-4:]}")
PY

echo
echo "===== 2. Clock skew (replay window is 300s) ====="
echo "  container utc : $($C exec -T api date -u +%s) ($($C exec -T api date -u))"
echo "  host utc      : $(date -u +%s) ($(date -u))"

echo
echo "===== 3. Why signatures were rejected (the specific reason) ====="
$C logs api 2>/dev/null | grep -i "intake signature rejected" | tail -10 \
  || echo "  (no rejection lines -- webhooks may never have arrived at all)"

echo
echo "===== 4. Did webhooks arrive but hit an unclaimed number? ====="
$C logs api 2>/dev/null | grep -i "unclaimed number" | tail -10 \
  || echo "  (none)"

echo
echo "===== 5. Any /v1/intake/webhook hits at all, and their status codes ====="
$C logs api 2>/dev/null | grep -oE '"POST /v1/intake/webhook HTTP/1\.1" [0-9]{3}' \
  | sort | uniq -c | sort -rn || echo "  (no webhook requests logged)"

echo
echo "===== 6. intake_numbers (org resolution depends on this) ====="
$C exec -T db psql -U juicebox -c \
  "select phone, org_id, active, created_at from intake_numbers order by created_at;"

echo
echo "===== 7. orgs, so a number can be registered to the right one ====="
$C exec -T db psql -U juicebox -c "select id, name from orgs order by created_at;"

echo
echo "===== 8. intake_calls: is anything stored at all? ====="
$C exec -T db psql -U juicebox -c \
  "select count(*) total, count(candidate_id) attributed from intake_calls;"
$C exec -T db psql -U juicebox -c \
  "select id, provider_call_id, entry_point, from_number, to_number, status,
          candidate_id, length(transcript) tlen, review_needed, created_at
     from intake_calls order by created_at desc limit 10;"

echo
echo "===== 9. candidate_phones: could an inbound caller ever be matched? ====="
$C exec -T db psql -U juicebox -c "select count(*) as candidate_phone_rows from candidate_phones;"

echo
echo "===== 10. how often the coverage gate ran on regexes ====="
# The mid-call extractor degrades to heuristics rather than failing the call,
# which is correct and was previously invisible: a deployment whose extractor
# times out on every call looked exactly like a healthy one. Anything above a
# few percent here means the gate is guessing what to ask next, and no model
# upgrade is worth judging until it is near zero.
$C exec -T db psql -U juicebox -c   "select count(*) filter (where review_reason like '%gate ran on heuristics%') as degraded,
          count(*) as calls_with_transcript,
          round(100.0 * count(*) filter (where review_reason like '%gate ran on heuristics%')
                / nullif(count(*), 0), 1) as pct
     from intake_calls where transcript <> '';"
$C exec -T db psql -U juicebox -c   "select id, from_number, created_at, review_reason
     from intake_calls
    where review_reason like '%gate ran on heuristics%'
    order by created_at desc limit 10;"

echo
echo "===== 11. extractor timeouts in the logs (the cause, if 10 is non-zero) ====="
$C logs api 2>/dev/null | grep -i "llm intake extraction failed" | tail -10   || echo "  none logged"

echo
echo "===== 11b. post-call extraction: did the worker actually run? ====="
# A call stuck at status "completed" with "No extraction yet" in the UI means
# the intake.extract job never finished -- which is a different failure from
# section 10's mid-call degradation. Three ways it happens: the worker
# container is down, the job is erroring through its five attempts, or the
# queue is backed up. This says which.
$C ps worker
$C exec -T db psql -U juicebox -c   "select kind, status, count(*), max(attempts) as max_attempts
     from jobs where kind like 'intake%' group by 1, 2 order by 1, 2;"
$C exec -T db psql -U juicebox -c   "select id, status, attempts, run_at, left(last_error, 120) as last_error
     from jobs where kind = 'intake.extract' and status not in ('done')
    order by run_at desc limit 5;" 2>/dev/null   || $C exec -T db psql -U juicebox -c   "select id, status, attempts, run_at
     from jobs where kind = 'intake.extract' and status not in ('done')
    order by run_at desc limit 5;"
$C logs worker 2>/dev/null | grep -iE "intake.extract|error|traceback" | tail -12   || echo "  no worker log lines"

echo
if [ -n "$CALLER" ]; then
echo "===== 12. why was $CALLER not recognised? ====="
# `resolve_candidate_id` reads candidate_phones and nothing else. So a number
# sitting in the `candidates.phones` JSON column but absent from the projection
# is a caller the dial-in line can never place -- and the two most common ways
# that happens are both silent: `sync_candidate_phones` skips a number another
# row already claimed (first writer wins), and `link_caller` deliberately never
# writes the number when it matched the caller by email. This section shows
# which of those it is rather than leaving it to be guessed at.
$C exec -T api python - "$CALLER" <<'PY'
import sys

from juicebox.db import get_session_factory
from juicebox.intake.phones import lookup_key

raw = sys.argv[1]
key = lookup_key(raw)
print(f"  raw input   : {raw}")
print(f"  lookup_key  : {key or '(none -- unmatchable, so never recognised)'}")
if not key:
    raise SystemExit(0)

db = get_session_factory()()
from sqlalchemy import select, text  # noqa: E402

from juicebox.models import Candidate, CandidatePhone  # noqa: E402

rows = db.scalars(select(CandidatePhone).where(CandidatePhone.phone == key)).all()
print(f"  candidate_phones rows for that key: {len(rows)}")
for row in rows:
    person = db.get(Candidate, row.candidate_id)
    name = person.full_name if person else "(missing candidate row)"
    print(f"     -> {row.candidate_id}  {name}  org={row.org_id}")
if not rows:
    print("     NOBODY CLAIMS IT. The dial-in line will treat this caller as new.")

owners = db.execute(
    text(
        "select id, full_name, org_id, phones::text from candidates "
        "where phones::text like :needle limit 10"
    ),
    {"needle": f"%{key[-10:]}%"},
).all()
print(f"  candidates whose phones JSON mentions it: {len(owners)}")
for cid, name, org, phones in owners:
    claimed = any(r.candidate_id == cid for r in rows)
    flag = "projected" if claimed else "NOT PROJECTED  <-- this is the bug"
    print(f"     -> {cid}  {name}  org={org}  {phones}  [{flag}]")

calls = db.execute(
    text(
        "select id, candidate_id, status, review_reason, contact_email, created_at "
        "from intake_calls where from_number = :n order by created_at desc limit 5"
    ),
    {"n": raw},
).all()
print(f"  recent calls from that number: {len(calls)}")
for cid, cand, status, reason, email, when in calls:
    print(f"     -> {when}  {cid}  candidate={cand or '(none)'}  {status}")
    print(f"        email={email or '(none captured)'}  reason={reason or '(none)'}")
PY
echo
fi
echo "===== done ====="
