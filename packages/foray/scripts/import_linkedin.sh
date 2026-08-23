#!/usr/bin/env bash
# One-off LinkedIn corpus import: import -> derive -> embed in chunks -> HNSW
# rebuild -> gate report. NOT a nightly phase: embedding 200k rows takes hours,
# which cannot live inside the 40-minute nightly embed budget.
#
#   FORAY_COMPOSE_FILE=... ./scripts/import_linkedin.sh /path/in/container.json
#
# Safe to re-run: the importer checkpoints and every write is idempotent.
# Holds its own flock so two imports cannot interleave; the importer's advisory
# lock (a different key from nightly's) additionally guards direct invocations.
# Schedule OUTSIDE the 23:00 UTC nightly window.
set -euo pipefail

FILE="${1:?usage: import_linkedin.sh /path/inside/foray/container.json}"
COMPOSE_FILE="${FORAY_COMPOSE_FILE:-/opt/juicebox/docker-compose.prod.yml}"
RUN=(docker compose -f "$COMPOSE_FILE" run --rm --no-deps -T foray-jobs)

exec 9>"/var/lock/foray-linkedin-import.lock"
flock -n 9 || { echo '{"event":"import.skipped","reason":"already running"}'; exit 0; }

echo "== import (resumable) =="
"${RUN[@]}" python -m foray.ingest.linkedin_file --path "$FILE" --resume

echo "== derive linkedin signals =="
"${RUN[@]}" python -m foray.enrich.derive --source linkedin

echo "== drop HNSW for the bulk load (rebuilt below) =="
docker compose -f "$COMPOSE_FILE" exec -T db \
    psql -U foray -d foray -c "drop index if exists ix_candidates_embedding;"

echo "== embed in chunks =="
# Each pass takes the next unembedded slice; loop until embed reports nothing
# left. --skip-index defers the HNSW build to the single rebuild below.
while :; do
    out="$("${RUN[@]}" python -m foray.embed.run --limit 20000 --skip-index | tail -1)"
    echo "$out"
    [[ "$out" == embedded\ 0\ * ]] && break
done

echo "== rebuild HNSW once =="
docker compose -f "$COMPOSE_FILE" exec -T db psql -U foray -d foray \
    -c "set maintenance_work_mem = '512MB';
        create index if not exists ix_candidates_embedding
            on candidates using hnsw (embedding halfvec_cosine_ops)
            with (m = 16, ef_construction = 64);"

echo "== gate report (read this BEFORE scoring: it prices the score run) =="
"${RUN[@]}" python -m foray.score.run --dry-run

echo "done. score with: python -m foray.score.run --yes --max-calls N"
