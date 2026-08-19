-- Indexes and autovacuum tuning for the unattended nightly pipeline at 100k.
-- migrate.sh runs each file in a single transaction, so no CREATE INDEX
-- CONCURRENTLY here.

-- The enrich error-backoff predicate (head-of-line starvation fix) and the
-- status error counts scan these; enrich_errors had no index.
create index if not exists ix_enrich_errors_candidate on enrich_errors (candidate_id, created_at desc);
create index if not exists ix_enrich_errors_created   on enrich_errors (created_at desc);
create index if not exists ix_scoring_errors_created  on scoring_errors (created_at desc);

-- embed's "changed only" default scan and derive's --stale-only join.
create index if not exists ix_candidates_unembedded on candidates (id) where embedding is null;
create index if not exists ix_signals_derived_at    on candidate_signals (derived_at);

-- Nightly UPDATEs rewrite whole candidate rows (776 B halfvec column); keep
-- autovacuum ahead of the bloat instead of waiting for the 20% default.
alter table candidates        set (autovacuum_vacuum_scale_factor = 0.05,
                                   autovacuum_analyze_scale_factor = 0.02);
alter table candidate_signals set (autovacuum_vacuum_scale_factor = 0.05);
