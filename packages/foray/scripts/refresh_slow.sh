#!/usr/bin/env bash
# Quarterly: full-pool re-derive + re-embed + rescore stale archetypes.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec 9>"/tmp/foray_refresh_slow.lock"
flock -n 9 || { echo "refresh_slow already running; skipping"; exit 0; }
PY="$HERE/../.venv/bin/python"
[ -x "$PY" ] || PY="python3"
cd "$HERE/.."
exec "$PY" -m foray.refresh.slow "$@"
