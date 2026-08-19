#!/bin/bash
# Runs once, on first initialization of the staging Postgres volume, as the
# superuser (POSTGRES_USER=juicebox). Creates a separate role + database for
# Foray so it shares the container but never shares a database name with prod
# (`foray` / `juicebox` are the production names; staging is always *_staging).
set -euo pipefail

password="${FORAY_DB_PASSWORD:-foray}"

if ! psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname = 'foray'" | grep -qx 1; then
    psql -v ON_ERROR_STOP=1 -v pwd="$password" \
        --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
        -c "CREATE ROLE foray WITH LOGIN PASSWORD :'pwd';"
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
SELECT 'CREATE DATABASE foray_staging OWNER foray'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'foray_staging')\gexec
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname foray_staging <<EOSQL
CREATE EXTENSION IF NOT EXISTS vector;
GRANT ALL ON SCHEMA public TO foray;
EOSQL
