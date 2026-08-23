-- 012: honest recency, honest experience, index admission control.
--
-- Recency. 005's header claimed activity_at was "a push for GitHub, the
-- current role's start ... or the export snapshot for LinkedIn". The LinkedIn
-- arm of that claim was false in effect: raw_profiles.fetched_at is stamped
-- now() by the importer on insert AND on conflict-update, so activity_at was
-- the IMPORT wall-clock, reset by every re-import. Measured on prod:
-- 196,778 / 196,778 LinkedIn rows passed the 24-month recency floor, all
-- stamped activity_kind='snapshot' inside one month -- the floor in the search
-- filter and in all 15 archetype gates was a no-op for 96% of the corpus.
--
-- activity_at is now EVIDENCE OR NULL. foray/ingest/activity.py decodes the
-- millisecond epoch out of LinkedIn's activity[].id feed URNs (bit-shift 22;
-- ~48.7% of records carry at least one decodable id), so the new kinds are
-- 'linkedin_activity' and 'linkedin_post'. No evidence -> NULL, and the
-- snapshot claim moves to profile_asof where it belongs. foray/recency.py is
-- the single three-valued predicate over the pair. 'snapshot' stays in the
-- CHECK: existing rows carry it until the re-derive rewrites them, and this
-- migration must not make them unreadable.
--
-- Experience. 134,536 rows carry est_experience_years = 0 meaning UNKNOWN (no
-- dated role in the profile), which every consumer read as "zero years".
-- Nullable now, so unknown looks unknown.
--
-- Admission. One flag, computed by enrich/derive.py's admission() and only
-- TESTED elsewhere (tsvector, embed selection, scoring queue, search filter).
-- The rule previously existed once, buried in score/common.py's
-- candidates_to_score, which is why 24,698 LinkedIn rows with no title, no
-- about-text and no experience entries still hold an embedding and a tsvector.
-- Default TRUE so this migration is a no-op until the re-derive runs: flipping
-- 200k rows to ineligible inside a schema migration would take the corpus
-- offline between the DDL and the derive.
--
-- Ordering: MUST run after 001..011 (001 creates candidate_signals with
-- est_experience_years NOT NULL DEFAULT 0; 005 adds activity_kind with its
-- CHECK). Post-migration steps live in the runbook, not here: re-derive, then
-- null search_tsv/embedding/embed_blob on the now-ineligible rows and
-- `reindex index concurrently ix_candidates_embedding` in a maintenance
-- window -- HNSW does not reclaim deleted vectors until rebuilt.

alter table candidate_signals
  add column if not exists search_eligible boolean not null default true,
  add column if not exists ineligible_reason text;

-- Unknown experience must be storable as unknown. The DEFAULT goes too: a
-- leftover `default 0` would let any writer that omits the column silently
-- recreate the unknown-as-zero population this is removing.
alter table candidate_signals
  alter column est_experience_years drop not null,
  alter column est_experience_years drop default;

-- activity_kind's CHECK must accept the new evidence kinds or the re-derive
-- fails on row 1. The constraint was declared inline in 005 (`add column ...
-- check (...)`), so its name is Postgres-generated. 011 relied on the default
-- name directly; here we look it up instead, because guessing wrong would not
-- error -- the drop would be a silent no-op, the ADD would succeed under a
-- suffixed name, and the stale constraint would still reject
-- 'linkedin_activity' at derive time.
do $$
declare
    c record;
begin
    for c in
        -- ::regclass resolves through search_path, so this cannot pick up a
        -- same-named table in another schema -- the loop body DROPs, so the
        -- lookup must not be able to widen.
        select con.conname
        from pg_constraint con
        where con.conrelid = 'candidate_signals'::regclass
          and con.contype = 'c'
          and pg_get_constraintdef(con.oid) ilike '%activity_kind%'
    loop
        execute format('alter table candidate_signals drop constraint %I', c.conname);
    end loop;
end
$$;

alter table candidate_signals add constraint candidate_signals_activity_kind_check
  check (activity_kind is null or activity_kind in (
      -- GitHub: the last push. LinkedIn: a decoded feed URN, or a dated post.
      'push', 'role', 'snapshot', 'linkedin_activity', 'linkedin_post'
  ));

-- profile_asof carries the second arm of the recency predicate now, so it is
-- read on every search. No index on search_eligible: it is a near-constant
-- boolean (the great majority of rows stay eligible), useless as a standalone
-- access path. The partial `where search_eligible` indexes on the facet
-- columns belong with the facets that need them.
create index if not exists ix_signals_profile_asof on candidate_signals (profile_asof);

-- Lets the runbook and any health probe count the ineligible population and
-- break it down by reason without a seq scan over 200k rows.
create index if not exists ix_signals_ineligible
  on candidate_signals (ineligible_reason)
  where not search_eligible;
