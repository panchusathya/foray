#!/usr/bin/env bash
# Tear down the staging stack. Default keeps the staging volume (restored
# dumps). --purge also drops the volume and the juicebox-staging-* images
# so they stop occupying disk next to the prod foray image.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE="$ROOT/docker-compose.staging.yml"
ENV_FILE="${STAGING_ENV:-$ROOT/.env.staging}"
PURGE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        -h|--help)
            echo "Usage: $(basename "$0") [--purge]"
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

compose() {
    if [[ -f "$ENV_FILE" ]]; then
        docker compose -f "$COMPOSE" --env-file "$ENV_FILE" "$@"
    else
        docker compose -f "$COMPOSE" "$@"
    fi
}

if [[ "$PURGE" -eq 1 ]]; then
    compose --profile foray --profile jobs --profile worker down -v --rmi local
    docker image prune -f
else
    compose --profile foray --profile jobs --profile worker down
fi
