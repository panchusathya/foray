#!/usr/bin/env bash
# Weekly: refresh volatile fields (employer/title/location/last_push) for the active subset.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec 9>"/tmp/foray_refresh_volatile.lock"
flock -n 9 || { echo "refresh_volatile already running; skipping"; exit 0; }
PY="$HERE/../.venv/bin/python"
[ -x "$PY" ] || PY="python3"
cd "$HERE/.."
exec "$PY" -m foray.refresh.volatile "$@"
