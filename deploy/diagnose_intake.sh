#!/usr/bin/env bash
# One-paste intake diagnosis, to be run ON THE VM from /opt/juicebox.
#
# Read-only. Prints no secrets: the Retell key is reported as a length and a
# sha256 prefix so it can be compared against the workspace key without either
# side pasting it. Everything here answers a question that cannot be answered
# from outside the box.
set -uo pipefail
C="docker compose -f docker-compose.prod.yml"

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
echo "===== done ====="
