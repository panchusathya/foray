#!/usr/bin/env bash
# Bulk LinkedIn backfill, Docker deployment. One instance at a time, host-wide.
set -euo pipefail
COMPOSE_FILE="${FORAY_COMPOSE_FILE:-/opt/juicebox/docker-compose.prod.yml}"
exec 9>"/var/lock/foray-backfill-linkedin.lock"
flock -n 9 || { echo '{"event":"backfill.skipped","reason":"already running"}'; exit 0; }
exec docker compose -f "$COMPOSE_FILE" exec -T foray python -m foray.enrich.linkedin_bulk "$@"
