#!/bin/sh
# Runs once, on first initialization of the staging Postgres volume (the
# official image only executes docker-entrypoint-initdb.d on an empty data
# dir). Creates the Foray role + foray_staging database so this cluster never
# uses the unsuffixed prod names. Keep this POSIX sh: the image may not have
# bash, and this script is not re-run so IF NOT EXISTS is unnecessary.
set -eu

psql -v ON_ERROR_STOP=1 -v pwd="${FORAY_DB_PASSWORD:-foray}" \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE foray WITH LOGIN PASSWORD :'pwd';
CREATE DATABASE foray_staging OWNER foray;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname foray_staging <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
GRANT ALL ON SCHEMA public TO foray;
SQL
