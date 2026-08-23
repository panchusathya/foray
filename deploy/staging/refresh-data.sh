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
# The PROD checkout, not this one. $ROOT/docker-compose.prod.yml exists in every
# checkout, but the prod compose file is full of ${VAR:?} requirements that
# resolve from the .env sitting NEXT TO IT -- and a staging checkout has no
# .env, only .env.staging. Defaulting to $ROOT therefore produced a pg_dump
# that could not interpolate, an empty restore, and (because the failure was
# inside a pipeline) an exit status of 0: the operator gets "refresh-data:
# done" over a database with zero rows, which is the precise outcome staging
# exists to prevent. Overridable for a VM that puts prod somewhere else.
PROD_ROOT="${PROD_ROOT:-/opt/juicebox}"
PROD_COMPOSE="${PROD_COMPOSE:-$PROD_ROOT/docker-compose.prod.yml}"
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

# Preflight the PROD side before dropping anything on the staging side. A
# compose file that cannot interpolate fails INSIDE the dump pipeline, where
# pipefail does not save us (psql happily consumes an empty stream and exits
# 0), so check it here where we can still refuse.
prod_env_hint="$(dirname "$PROD_COMPOSE")/.env"
docker compose -f "$PROD_COMPOSE" config >/dev/null 2>&1 || die \
    "cannot interpolate $PROD_COMPOSE (expected its .env at $prod_env_hint).
  This is the prod checkout's compose file, not this checkout's copy.
  Point at it explicitly:  PROD_ROOT=/opt/juicebox $(basename "$0") --migrate"

# Row counts must be non-zero afterwards, or the restore silently did nothing.
assert_restored() {
    local db="$1" table="$2" n
    n="$(compose_staging exec -T db psql -U juicebox -d "$db" -tAc \
        "select count(*) from $table" 2>/dev/null || echo 0)"
    [[ "${n:-0}" -gt 0 ]] || die \
        "restore into $db left $table empty (count=$n). The dump produced
  nothing -- check that $PROD_COMPOSE interpolates and that prod's db
  container is running. NOT reporting success over an empty database."
    echo "refresh-data: $db.$table = $n rows"
}

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

# GRANT is not enough: ALTER TABLE requires OWNERSHIP, and no privilege confers
# it. Without this, `--migrate` dies on the first foray migration that adds a
# column -- "must be owner of table candidate_signals" -- which means the one
# thing staging exists for (rehearsing a schema change against
# production-shaped data) is the one thing it could not do. In prod the foray
# role owns these tables because it created them; --no-owner lands them under
# juicebox, so hand them over explicitly.
compose_staging exec -T db psql -U juicebox -d "$FORAY_DB" -v ON_ERROR_STOP=1 <<'SQL'
do $$
declare r record; n int := 0;
begin
  for r in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I owner to foray', r.tablename); n := n + 1;
  end loop;
  for r in select sequencename from pg_sequences where schemaname = 'public' loop
    execute format('alter sequence public.%I owner to foray', r.sequencename); n := n + 1;
  end loop;
  for r in select matviewname from pg_matviews where schemaname = 'public' loop
    execute format('alter materialized view public.%I owner to foray', r.matviewname);
    n := n + 1;
  end loop;
  raise notice 'refresh-data: reassigned % objects in public to foray', n;
end $$;
SQL

assert_restored "$JUICEBOX_DB" orgs
assert_restored "$FORAY_DB" candidates

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
