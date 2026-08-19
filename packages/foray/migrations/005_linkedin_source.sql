-- 005: fold LinkedIn-only people into the same pool as GitHub people.
--
-- Identity: LinkedIn rows get login = 'li:' || slug. ':' is illegal in GitHub
-- logins so collision is impossible, and it keeps login NOT NULL UNIQUE as the
-- importer's free idempotency key. `source` is a real column (not a login
-- sniff) because the API must stop emitting a dead github_url for them.
--
-- Recency: every archetype gate and the search floor filtered on
-- last_push_at, which no LinkedIn row will ever have. `activity_at` is the
-- evidence-agnostic replacement: a push for GitHub, the current role's start
-- (they are employed *now*) or the export snapshot for LinkedIn, with
-- activity_kind recording which claim it is. Backfilled here, in the same
-- transaction as the search swap that reads it.

alter table candidates
  add column if not exists source text not null default 'github'
      check (source in ('github', 'linkedin', 'both')),
  add column if not exists linkedin_slug text,
  add column if not exists merged_into bigint references candidates(id);
create unique index if not exists ux_candidates_linkedin_slug
    on candidates (linkedin_slug);
create index if not exists ix_candidates_source on candidates (source);
create index if not exists ix_candidates_merged
    on candidates (merged_into) where merged_into is not null;

-- One person may hold BOTH payloads (a merged GitHub+LinkedIn identity).
alter table raw_profiles
  add column if not exists source text not null default 'github'
      check (source in ('github', 'linkedin'));
alter table raw_profiles drop constraint raw_profiles_pkey;
alter table raw_profiles add primary key (candidate_id, source);

alter table candidate_signals
  add column if not exists evidence_kind text not null default 'github'
      check (evidence_kind in ('github', 'linkedin', 'both')),
  add column if not exists activity_at timestamptz,
  add column if not exists activity_kind text
      check (activity_kind in ('push', 'role', 'snapshot')),
  add column if not exists current_role_start_at timestamptz,
  add column if not exists tenure_months int,
  add column if not exists profile_asof timestamptz;
update candidate_signals
   set activity_at  = last_push_at,
       activity_kind = case when last_push_at is null then null else 'push' end
 where activity_at is null;
create index if not exists ix_signals_activity on candidate_signals (activity_at);
create index if not exists ix_signals_evidence on candidate_signals (evidence_kind);

-- Business emails from the export land at confidence 80: below a verified
-- Apollo work record (85), above a commit-mined address (75).
alter table candidate_emails drop constraint candidate_emails_source_check;
alter table candidate_emails add constraint candidate_emails_source_check
  check (source in ('profile', 'commit', 'apollo_work', 'apollo_personal', 'guess', 'linkedin'));

-- Entity resolution: reversible merges, never deletion. The loser keeps its
-- row with merged_into set; every read path filters merged_into is null.
create table if not exists candidate_merges (
    id          bigint generated always as identity primary key,
    primary_id  bigint not null references candidates(id) on delete cascade,
    merged_id   bigint not null references candidates(id) on delete cascade,
    tier        text not null
                check (tier in ('linkedin_url', 'business_email', 'name_domain', 'name_metro')),
    status      text not null default 'pending'
                check (status in ('pending', 'applied', 'rejected', 'reverted')),
    evidence    jsonb not null default '{}'::jsonb,
    decided_by  text,
    created_at  timestamptz not null default now(),
    decided_at  timestamptz
);
create index if not exists ix_merges_status on candidate_merges (status, tier);

-- Resumable bulk imports: byte-offset checkpoints keyed by file fingerprint.
create table if not exists import_checkpoints (
    file_key      text primary key,
    path          text not null,
    records_done  bigint not null default 0,
    byte_offset   bigint not null default 0,
    rows_upserted bigint not null default 0,
    finished_at   timestamptz,
    updated_at    timestamptz not null default now()
);

-- The active set's top-decile arm would be recomputed against a pool 15x
-- larger and could flip to mostly LinkedIn people, starving real GitHub
-- candidates of the volatile/event refresh (whose GraphQL calls only make
-- sense for GitHub identities anyway). Scope the score arm to GitHub evidence;
-- the shortlist arm stays open to everyone a human picked.
drop materialized view if exists active_candidates;
create materialized view active_candidates as
select c.id, c.login
from candidates c
where c.merged_into is null
  and (c.id in (select candidate_id from shortlist_events)
   or (c.source in ('github', 'both') and c.id in (
        select a.candidate_id
        from (
            select candidate_id,
                   percent_rank() over (partition by archetype_id order by score desc) as pr
            from archetype_scores
        ) a
        where a.pr <= 0.1
   )));
create unique index if not exists ix_active_candidates_id on active_candidates (id);
