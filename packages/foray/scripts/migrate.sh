#!/usr/bin/env bash
# Apply any migrations/*.sql not yet recorded in schema_migrations, in filename
# order, each inside a single transaction. Idempotent: a second run is a no-op.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIG_DIR="$HERE/../migrations"
DB_URL="${FORAY_DATABASE_URL:-postgresql://foray:foray@localhost:5432/foray}"

if ! command -v psql >/dev/null 2>&1; then
    echo "migrate: psql not found on PATH (install postgresql-client)" >&2
    exit 1
fi

psql "$DB_URL" -v ON_ERROR_STOP=1 -q -c \
    "create table if not exists schema_migrations (
        version text primary key,
        applied_at timestamptz not null default now()
     );"

applied="$(psql "$DB_URL" -t -A -c "select version from schema_migrations;")"

shopt -s nullglob
for f in "$MIG_DIR"/*.sql; do
    v="$(basename "$f")"
    if grep -qxF "$v" <<<"$applied"; then
        echo "skip  $v"
        continue
    fi
    echo "apply $v"
    psql "$DB_URL" -v ON_ERROR_STOP=1 --single-transaction \
        -f "$f" \
        -c "insert into schema_migrations (version) values ('$v');"
done

echo "migrations up to date"
