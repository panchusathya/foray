-- Lexical search arm: a plain tsvector on candidate_signals.
--
-- ADD COLUMN nullable, no default is catalog-only in PG 16 (no rewrite), so
-- the GIN index builds over an all-NULL column instantly. migrate.sh runs
-- each file in a single transaction, so CREATE INDEX CONCURRENTLY is not
-- used here. A GENERATED STORED column would have rewritten 200k rows inside
-- that transaction — the concrete reason this is a plain column.
--
-- Backfill is a separate script (scripts/backfill_search_tsv.py) that never
-- touches derived_at. Until it runs, search_tsv is NULL everywhere and the
-- lexical arm matches nothing — search degrades to vector-only, never to
-- wrong results.
--
-- PARSER_VERSION in parse.py makes existing query_parse_cache rows unreachable;
-- truncate so they do not accumulate as orphans.

alter table candidate_signals
    add column if not exists search_tsv tsvector;

create index if not exists ix_signals_search_tsv
    on candidate_signals using gin (search_tsv);

truncate query_parse_cache;
