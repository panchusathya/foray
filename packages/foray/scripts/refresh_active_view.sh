#!/usr/bin/env bash
# Nightly: recompute the active_candidates materialized view.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec 9>"/tmp/foray_refresh_active_view.lock"
flock -n 9 || { echo "refresh_active_view already running; skipping"; exit 0; }
PY="$HERE/../.venv/bin/python"
[ -x "$PY" ] || PY="python3"
cd "$HERE/.."
exec "$PY" -m foray.refresh.active_view "$@"
