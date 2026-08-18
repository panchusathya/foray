#!/usr/bin/env bash
# Daily: job-change detection over the watchlist (active subset).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec 9>"/tmp/foray_refresh_events.lock"
flock -n 9 || { echo "refresh_events already running; skipping"; exit 0; }
PY="$HERE/../.venv/bin/python"
[ -x "$PY" ] || PY="python3"
cd "$HERE/.."
exec "$PY" -m foray.refresh.events "$@"
