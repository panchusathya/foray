#!/usr/bin/env bash
# Bring up the staging stack from this checkout. Does not touch the prod
# compose project. Building juicebox-staging-* images will not retag
# juicebox-foray, but it is CPU-heavy — refuse to start inside the nightly
# window, and warn if the prod contacts chain is running.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE="$ROOT/docker-compose.staging.yml"
ENV_FILE="${STAGING_ENV:-$ROOT/.env.staging}"
PROFILES=()

usage() {
    cat <<EOF
Usage: $(basename "$0") [--foray] [--worker] [--force]

  Start db + api + web on 127.0.0.1:18080. Add --foray to also build and
  start the Foray API (RAM-heavy on the prod VM). --worker starts the
  Juicebox worker (still EMAIL_PROVIDER=console).

  To rehearse the nightly orchestrator against the staging database, do not
  pass a flag here — after the stack is up:

    docker compose -f docker-compose.staging.yml --env-file .env.staging \\
      --profile jobs run --rm --no-deps -T foray-jobs

  Refuses to run between 22:00 and 00:30 UTC (prod nightly is 23:00) unless
  --force is set. Does not recreate prod containers.
EOF
}

FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --foray) PROFILES+=(--profile foray); shift ;;
        --worker) PROFILES+=(--profile worker); shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

die() { echo "staging-up: $*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE (cp .env.staging.example .env.staging)"

hour_min="$(date -u +%H%M)"
if [[ "$FORCE" -eq 0 && ( "$hour_min" > "2159" || "$hour_min" < "0030" ) ]]; then
    die "inside the prod nightly window (23:00 UTC). Wait, or pass --force"
fi

if pgrep -f foray.enrich >/dev/null 2>&1; then
    echo "staging-up: warning: foray.enrich is running in prod. This script will not recreate that container, but a Foray image build is CPU-heavy — prefer --force only if you meant this." >&2
    if [[ "$FORCE" -eq 0 && " ${PROFILES[*]} " == *" foray "* ]]; then
        die "refusing --foray while foray.enrich is running (pgrep -f foray.enrich). Pass --force to override"
    fi
fi

exec docker compose -f "$COMPOSE" --env-file "$ENV_FILE" "${PROFILES[@]}" up -d --build
