#!/usr/bin/env bash
# Copy production databases into the staging Postgres.
#
# Restores into juicebox_staging / foray_staging only. Refuses to run if the
# staging db container is the same as prod, or if the target database names
# do not end in _staging. Does not recreate any prod container — safe to run
# while the contacts chain is in the prod foray container, though it is IO
# heavy; skip it during the 23:00 UTC nightly window.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROD_COMPOSE="${PROD_COMPOSE:-$ROOT/docker-compose.prod.yml}"
STAGING_COMPOSE="${STAGING_COMPOSE:-$ROOT/docker-compose.staging.yml}"
STAGING_ENV="${STAGING_ENV:-$ROOT/.env.staging}"
JUICEBOX_DB="${JUICEBOX_STAGING_DB:-juicebox_staging}"
FORAY_DB="${FORAY_STAGING_DB:-foray_staging}"
MIGRATE=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [--migrate] [--prod-compose FILE] [--staging-compose FILE]

  Restore the live juicebox + foray databases into the staging Postgres, then
  optionally apply this checkout's migrations on top (the reason staging
  exists: rehearse a schema change against production-shaped data).

  --migrate   run juicebox alembic + foray migrate.sh after the restore
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --migrate) MIGRATE=1; shift ;;
        --prod-compose) PROD_COMPOSE="$2"; shift 2 ;;
        --staging-compose) STAGING_COMPOSE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

die() { echo "refresh-data: $*" >&2; exit 1; }

[[ "$JUICEBOX_DB" == *_staging ]] || die "refusing to restore into '$JUICEBOX_DB' (name must end in _staging)"
[[ "$FORAY_DB" == *_staging ]] || die "refusing to restore into '$FORAY_DB' (name must end in _staging)"

compose_staging() {
    if [[ -f "$STAGING_ENV" ]]; then
        docker compose -f "$STAGING_COMPOSE" --env-file "$STAGING_ENV" "$@"
    else
        docker compose -f "$STAGING_COMPOSE" "$@"
    fi
}

prod_id="$(docker compose -f "$PROD_COMPOSE" ps -q db 2>/dev/null || true)"
staging_id="$(compose_staging ps -q db 2>/dev/null || true)"
[[ -n "$staging_id" ]] || die "staging db is not running (start it with deploy/staging/up.sh)"
if [[ -n "$prod_id" && "$prod_id" == "$staging_id" ]]; then
    die "staging db container is the same as prod — aborting"
fi

staging_name="$(docker inspect -f '{{.Name}}' "$staging_id")"
[[ "$staging_name" == *staging* ]] || die "staging db container '$staging_name' does not look like staging"

echo "refresh-data: dumping prod juicebox -> $JUICEBOX_DB"
compose_staging exec -T db psql -U juicebox -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$JUICEBOX_DB' AND pid <> pg_backend_pid();" \
    -c "DROP DATABASE IF EXISTS $JUICEBOX_DB;" \
    -c "CREATE DATABASE $JUICEBOX_DB OWNER juicebox;"
docker compose -f "$PROD_COMPOSE" exec -T db pg_dump -U juicebox --no-owner --no-acl juicebox \
    | compose_staging exec -T db psql -U juicebox -d "$JUICEBOX_DB" -v ON_ERROR_STOP=1 -q

echo "refresh-data: dumping prod foray -> $FORAY_DB"
compose_staging exec -T db psql -U juicebox -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$FORAY_DB' AND pid <> pg_backend_pid();" \
    -c "DROP DATABASE IF EXISTS $FORAY_DB;" \
    -c "CREATE DATABASE $FORAY_DB OWNER foray;"
compose_staging exec -T db psql -U juicebox -d "$FORAY_DB" -v ON_ERROR_STOP=1 \
    -c "CREATE EXTENSION IF NOT EXISTS vector;" \
    -c "GRANT ALL ON SCHEMA public TO foray;"
docker compose -f "$PROD_COMPOSE" exec -T db pg_dump -U juicebox --no-owner --no-acl foray \
    | compose_staging exec -T db psql -U juicebox -d "$FORAY_DB" -v ON_ERROR_STOP=1 -q
# --no-owner loads the dump as juicebox; the Foray app connects as foray.
compose_staging exec -T db psql -U juicebox -d "$FORAY_DB" -v ON_ERROR_STOP=1 \
    -c "GRANT ALL ON SCHEMA public TO foray;" \
    -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO foray;" \
    -c "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO foray;" \
    -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO foray;" \
    -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO foray;"

if [[ "$MIGRATE" -eq 1 ]]; then
    echo "refresh-data: applying this checkout's migrations"
    compose_staging exec -T api python -m juicebox.cli migrate
    if compose_staging ps --status running -q foray >/dev/null 2>&1; then
        compose_staging exec -T foray ./scripts/migrate.sh
    else
        echo "refresh-data: foray service is not running; juicebox migrated. Start --profile foray to migrate foray_staging."
    fi
fi

echo "refresh-data: done"
