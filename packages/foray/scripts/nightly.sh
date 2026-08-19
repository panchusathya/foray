#!/usr/bin/env bash
# Unattended nightly growth pipeline. One instance at a time, host-wide.
#
# Install as a host cron entry (see scripts/crontab.example). The orchestrator
# also takes a Postgres advisory lock, so even a manual `docker compose run`
# that bypasses this flock cannot double-run the pipeline.
set -euo pipefail
COMPOSE_FILE="${FORAY_COMPOSE_FILE:-/opt/juicebox/docker-compose.prod.yml}"

exec 9>"/var/lock/foray-nightly.lock"   # NOT /tmp: /tmp is per-container under Docker
flock -n 9 || { echo '{"event":"nightly.skipped","reason":"already running"}'; exit 0; }

exec docker compose -f "$COMPOSE_FILE" run --rm --no-deps -T foray-jobs \
     python -m foray.refresh.nightly "$@"
