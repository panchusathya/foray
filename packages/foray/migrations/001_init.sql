-- Foray initial schema.
--
-- Authored from the build-plan spec (the referenced external schema.sql was not
-- attached). The one decision that is expensive to reverse is kept verbatim from
-- the spec: archetype scores live in a normalized child table (archetype_scores),
-- not wide columns on candidates. See the block comment above that table.
--
-- Deviation from the bare DDL snippet in the plan: archetype_scores also stores
-- `highlights` and `flags` (jsonb). Phase 5 returns "precomputed highlights" with
-- "nothing to wait for" in the query path, so they must be persisted here.

create extension if not exists vector;

-- ---------------------------------------------------------------- candidates --
-- Lean identity + retrieval columns. Profile-derived facts live in
-- candidate_signals; the full GitHub payload lives in raw_profiles.
create table if not exists candidates (
    id                  bigint generated always as identity primary key,
    login               text not null unique,
    github_id           bigint unique,
    discovery_shard     text,                 -- which harvest shard first found them
    name                text,
    location_raw        text,                 -- kept forever, never normalized destructively

    -- embeddings (Phase 3)
    embed_blob          text,                 -- the exact text embedded; diff to skip re-embed
    embedding           halfvec(384),
    embedding_model     text,
    embedded_at         timestamptz,

    -- lazy LinkedIn resolution (Phase 6)
    linkedin_url        text,
    linkedin_source     text,                 -- bio | readme | website | apollo
    linkedin_checked_at timestamptz,

    -- freshness (Phase 6)
    enriched_at         timestamptz,          -- last full GraphQL enrich
    freshness_at        timestamptz,          -- last on-click freshness check

    created_at          timestamptz not null default now()
);

-- ------------------------------------------------------------- harvest_shards --
-- The query space is partitioned into shards each expected to yield < 1000
-- results (the GitHub Search API hard cap). The worker claims shards with
-- SELECT ... FOR UPDATE SKIP LOCKED.
create table if not exists harvest_shards (
    id             bigint generated always as identity primary key,
    location       text not null,
    language       text not null,
    followers      text not null,   -- e.g. "10..24", ">=1000"
    created_range  text not null,   -- e.g. "2012-01-01..2012-12-31"
    query          text not null,
    status         text not null default 'pending'
                   check (status in ('pending', 'running', 'done', 'truncated')),
    total_count    int,
    pages_done     int not null default 0,
    found          int not null default 0,
    parent_id      bigint references harvest_shards(id) on delete set null,
    lease_expires_at timestamptz,
    error          text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (location, language, followers, created_range)
);

create index if not exists ix_shards_status on harvest_shards (status);
create index if not exists ix_shards_lease on harvest_shards (status, lease_expires_at);

-- --------------------------------------------------------------- raw_profiles --
-- Raw GraphQL response per candidate. Signals are always re-derivable from here
-- with zero API calls, so signal logic can change without refetching.
create table if not exists raw_profiles (
    candidate_id  bigint primary key references candidates(id) on delete cascade,
    login         text not null,
    data          jsonb not null,
    fetched_at    timestamptz not null default now()
);

-- ----------------------------------------------------------- candidate_signals --
create table if not exists candidate_signals (
    candidate_id          bigint primary key references candidates(id) on delete cascade,

    employer              text,
    employer_source       text,       -- company | org | email_domain | null
    title                 text,

    location_metro        text,        -- nullable; location_raw stays on candidates

    language_mix          jsonb not null default '{}'::jsonb,
    primary_language      text,
    secondary_language    text,

    total_stars           int not null default 0,
    max_repo_stars        int not null default 0,
    non_fork_repo_count   int not null default 0,
    archived_fraction     real not null default 0,

    commits_last_year     int not null default 0,
    prs_last_year         int not null default 0,
    last_push_at          timestamptz,

    account_age_years     real not null default 0,
    est_experience_years  real not null default 0,
    experience_confidence text not null default 'low'
                          check (experience_confidence in ('high', 'medium', 'low')),

    domain_markers        text[] not null default '{}',
    oss_maintainer        boolean not null default false,

    top_repos             jsonb not null default '[]'::jsonb,  -- for blob + API top_repos
    topics                text[] not null default '{}',

    derived_at            timestamptz not null default now()
);

create index if not exists ix_signals_markers on candidate_signals using gin (domain_markers);
create index if not exists ix_signals_last_push on candidate_signals (last_push_at);
create index if not exists ix_signals_experience on candidate_signals (est_experience_years);
create index if not exists ix_signals_primary_lang on candidate_signals (primary_language);
create index if not exists ix_signals_metro on candidate_signals (location_metro);

-- ----------------------------------------------------------------- archetypes --
-- The effective rubric is (rubric + "\n\n" + shared_rubric_tail), concatenated
-- in Python. rubric_hash is the sha256 of that concatenated text.
create table if not exists archetypes (
    id          text primary key,
    label       text not null,
    gate        text not null,     -- SQL boolean over candidate_signals/candidates
    rubric      text not null,     -- effective (concatenated) rubric text
    rubric_hash text not null,
    updated_at  timestamptz not null default now()
);

-- ------------------------------------------------------------ archetype_scores --
-- Normalized child table, NOT wide columns on candidates:
--   * adding an archetype is an INSERT, not an ALTER TABLE + backfill;
--   * rubric_hash lets us detect and rescore exactly the stale rows;
--   * partial indexes per archetype are possible and fast;
--   * sparsity is the norm (most candidates are gated out of most archetypes).
create table if not exists archetype_scores (
    candidate_id  bigint not null references candidates(id) on delete cascade,
    archetype_id  text   not null references archetypes(id),
    score         smallint not null check (score between 0 and 100),
    highlights    jsonb  not null default '[]'::jsonb,   -- the product; rendered
    rationale     text   not null,                       -- internal; eval/debug only
    flags         jsonb  not null default '[]'::jsonb,    -- surfaced as caveat chips
    rubric_hash   text   not null,
    model         text   not null,
    scored_at     timestamptz not null default now(),
    primary key (candidate_id, archetype_id)
);

-- Supports the Phase 5 hot path: WHERE archetype_id = $1 ORDER BY score DESC.
create index if not exists ix_scores_archetype_score
    on archetype_scores (archetype_id, score desc);

-- ------------------------------------------------------- errors + observability --
create table if not exists enrich_errors (
    id           bigint generated always as identity primary key,
    candidate_id bigint,
    login        text,
    stage        text not null,       -- fetch | derive
    error        text not null,
    created_at   timestamptz not null default now()
);

create table if not exists scoring_errors (
    id           bigint generated always as identity primary key,
    candidate_id bigint,
    archetype_id text,
    custom_id    text,
    raw_response text,
    error        text not null,
    created_at   timestamptz not null default now()
);

create table if not exists refresh_runs (
    id           bigint generated always as identity primary key,
    job          text not null,
    started_at   timestamptz not null default now(),
    ended_at     timestamptz,
    rows_touched int not null default 0,
    api_calls    int not null default 0,
    errors       int not null default 0,
    notes        text
);

-- Query-parse cache (Phase 5): repeat searches skip the single LLM parse call.
create table if not exists query_parse_cache (
    query_hash text primary key,
    raw_query  text not null,
    parsed     jsonb not null,
    created_at timestamptz not null default now()
);

-- Shortlist log (feeds the Phase 7 active_candidates materialized view, and
-- gates Apollo spend to an explicit shortlist context).
create table if not exists shortlist_events (
    id           bigint generated always as identity primary key,
    candidate_id bigint not null references candidates(id) on delete cascade,
    archetype_id text,
    reason       text not null,
    created_at   timestamptz not null default now()
);

create index if not exists ix_shortlist_candidate on shortlist_events (candidate_id);
