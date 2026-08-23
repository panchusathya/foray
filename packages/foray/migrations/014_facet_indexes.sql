-- 014: access paths for the wave-1 Boolean facet layer.
--
-- Why these shapes and not `ilike any(array[...])`: measured on prod, a single
-- metro predicate written as `c.location_raw ilike any(array[...])` was a
-- ScalarArrayOp, which cannot use a trigram GIN index -- seq scan, 38k
-- buffers, 219 ms -- and _filter_clauses is rebuilt for the count, vector,
-- lexical and enrich statements, so ~4x that per request. api/search.py now
-- emits an OR-chain of individual LIKEs, which the planner CAN bitmap-OR
-- across the indexes below. If a future edit collapses a text facet back into
-- `any()`, these indexes go unused and nothing errors.
--
-- scripts/migrate.sh runs each file inside ONE transaction, so CREATE INDEX
-- CONCURRENTLY is not available here (it cannot run in a transaction block) --
-- the same constraint 006 documented for the tsvector GIN. At 204,535 rows
-- these build in seconds; the write lock is not worth a second migration
-- mechanism. Every index is PARTIAL on the admission flag, which is in the
-- shared WHERE of every retrieval statement, so the partial index is usable by
-- all of them and holds only the retrievable population.
--
-- Not here, deliberately: seniority_rank, mgmt_rank and the text[] columns
-- (past_employers, role_titles, schools, ...). Those columns and their indexes
-- ship in migration 013 with the derive that populates them.

-- pg_trgm is a TRUSTED extension in PG13+, so the unprivileged `foray` role
-- can create it -- no superuser step needed. That is why this is here rather
-- than in docker/initdb/00-foray.sql, which exists only because `vector` is
-- NOT trusted and must be created by the container's superuser on first init.
-- Its own statement, before anything that depends on gin_trgm_ops.
create extension if not exists pg_trgm;

-- Title and employer substring search: `lower(s.title) like '%ml engineer%'`.
-- The expression must match the query's expression EXACTLY (lower(col), no
-- collation qualifier) or the planner will not use the index.
create index if not exists ix_signals_title_trgm
    on candidate_signals using gin (lower(title) gin_trgm_ops)
    where search_eligible;

create index if not exists ix_signals_employer_trgm
    on candidate_signals using gin (lower(employer) gin_trgm_ops)
    where search_eligible;

-- The canonical fast path: for the ~44 companies in data/company_aliases.yaml,
-- derive stores exactly one spelling, so _filter_clauses emits
-- `lower(s.employer) = any($n)` and a btree beats the trigram scan.
create index if not exists ix_signals_employer_lower
    on candidate_signals (lower(employer))
    where search_eligible;

-- Metro's raw-text arm. location_raw lives on `candidates`, not
-- candidate_signals, so this index CANNOT be partial on search_eligible --
-- that column is on the other table. `merged_into is null` is the
-- candidates-side predicate that is in the same shared WHERE, so it is the
-- correct partial condition here; the eligibility filter is applied from the
-- joined signals row.
create index if not exists ix_candidates_location_raw_trgm
    on candidates using gin (lower(location_raw) gin_trgm_ops)
    where merged_into is null;

-- Array overlap for the topics facet (`s.topics && $n::text[]`). Default
-- array_ops, no extension involved.
create index if not exists ix_signals_topics
    on candidate_signals using gin (topics)
    where search_eligible;

create index if not exists ix_signals_tenure_months
    on candidate_signals (tenure_months)
    where search_eligible;

-- PARSER_VERSION 5 plus vocab_hash() in the cache key already make every
-- existing row unreachable; truncate so they do not sit there as orphans.
-- Same reasoning as 006_lexical_search.sql:23. Cache rows are regenerable by
-- definition -- the cost of this line is one LLM parse per distinct query.
truncate query_parse_cache;
