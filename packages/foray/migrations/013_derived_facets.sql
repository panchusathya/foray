-- 013: derive-time facet columns. Eleven columns, computed by
-- enrich/derive.py and only READ everywhere else.
--
-- MUST run after 001..012. 001 creates candidate_signals (including
-- location_metro, whose MEANING changes here -- see below); 012 adds
-- search_eligible, which every index in this file is partial on.
--
-- No table rewrite. Every ADD COLUMN below is either nullable-with-no-default
-- or defaulted with a non-volatile constant, both of which PG16 records in the
-- catalog only (pg_attribute.atthasmissing / attmissingval) rather than
-- rewriting 204,535 rows -- the same property 006 relies on for search_tsv.
-- migrate.sh runs each file in ONE transaction, so a rewrite here would hold
-- an ACCESS EXCLUSIVE lock on the corpus for the duration; catalog-only DDL
-- takes the same lock for milliseconds.
--
-- Until the re-derive runs, the new columns are NULL / '{}' everywhere and
-- every facet built on them matches nothing rather than matching wrongly. The
-- §9 unknown policy is what makes that safe: unknown passes by default.
--
-- WHY DERIVE-TIME AT ALL. These are all computable at query time from title
-- and the raw payload, and doing so was the status quo for zero of them --
-- because you cannot range-scan a regex. An ordinal smallint on a btree
-- answers `seniority_rank >= 4`; a text[] on a GIN answers "ever worked at
-- Google"; both are computed 204,535 times offline instead of once per row per
-- query, and both are unit-testable against the real title distribution
-- (tests/test_seniority.py, tests/test_linkedin_map.py).
--
-- THE GOVERNING RULE, restated because these columns are where it bites:
-- every facet needs a source-aware unknown policy or it becomes a source
-- filter in disguise. Measured on prod: title is populated on ZERO of the
-- 7,757 GitHub rows, and primary_language/topics on ZERO of the 196,778
-- LinkedIn rows. So seniority_rank is filled from a TITLE where there is one
-- and from an experience PROXY where there is not, and seniority_source says
-- which -- without that column, `seniority_rank >= 4` would have silently
-- deleted the entire GitHub half of the corpus, exactly as
-- `primary_language = any(...)` deletes the LinkedIn half today (550 rows
-- matched for "python", 0.3% of the corpus).

alter table candidate_signals
  -- The IC ladder: 1 intern, 2 entry/junior, 3 mid, 4 senior,
  -- 5 staff/lead/principal, 6 distinguished/fellow. NULL = UNKNOWN, never
  -- "junior" -- 012 removed the same unknown-as-zero confusion from
  -- est_experience_years, where 134,536 rows stored 0 meaning "no dated role"
  -- and every consumer read it as "zero years of experience". smallint, not
  -- int: it is an ordinal with six values and it will sit in composite index
  -- tuples.
  add column if not exists seniority_rank smallint,

  -- The MANAGEMENT ladder: 1 manager, 2 director, 3 vp, 4 cxo/chief.
  -- NULL = not management, which is the common case, so `mgmt_rank is not
  -- null` IS the is_manager predicate and no extra boolean is needed.
  --
  -- SEPARATE from seniority_rank on purpose. "Engineering Manager" is not
  -- "senior IC", and collapsing the two ladders into one number is precisely
  -- what makes a seniority filter useless: a recruiter asking for a staff
  -- engineer does not want directors. A hybrid title ("Senior Engineering
  -- Manager") fills BOTH, and a consumer wanting pure ICs writes
  -- `mgmt_rank is null`.
  add column if not exists mgmt_rank smallint,

  -- NOT NULL DEFAULT false, not a nullable boolean. For a filter, "no evidence
  -- you founded a company" and "you did not found a company" are the same
  -- answer, and a three-valued boolean would make `is_founder = false`
  -- silently drop the unknowns.
  add column if not exists is_founder boolean not null default false,

  -- 'title' | 'experience_proxy' | NULL. The provenance column that keeps the
  -- proxy from masquerading as a title reading. A consumer that must not
  -- conflate them filters `seniority_source = 'title'`; one that wants recall
  -- accepts both. merge_signals guarantees a title reading is NEVER
  -- downgraded to a proxy on a row that has both payloads.
  add column if not exists seniority_source text
      check (seniority_source is null
             or seniority_source in ('title', 'experience_proxy')),

  -- Career history. Role.current has been computed in
  -- ingest/linkedin_map.flatten_experience since day one and read only by
  -- current_role_start, so its COMPLEMENT -- where a person used to work --
  -- was discarded on every one of 196,778 profiles and "ex-Google" was
  -- unanswerable. Capped at 12 entries in the deriver so one pathological
  -- profile cannot dominate a GIN index page.
  add column if not exists past_employers text[] not null default '{}',

  -- title holds the best ONE title. These hold every title held, and the
  -- currently-held subset: "is a staff engineer" and "has ever been a staff
  -- engineer" are different questions and one column cannot answer both.
  -- Both are junk-filtered through the same _junk_role_title as best_title,
  -- so the SET/LinkedIn phantom (5,462 prod rows) cannot re-enter here.
  add column if not exists role_titles text[] not null default '{}',
  add column if not exists current_titles text[] not null default '{}',

  -- Education. MEASURED coverage on data/in/linkedin_sample.jsonl (2,000
  -- records, 2,118 education entries): at least one school on 55.1% of
  -- records, a degree on 6.5%, a field of study on 5.2%. schools is a real
  -- facet; the other two are near-empty ON THIS EXPORT and are stored because
  -- they cost one array each and a richer export fills them for free.
  -- Do NOT ship a hard degree/field predicate on these numbers: a filter
  -- matching 6% of the corpus deletes the 94% that never carried the field --
  -- the same failure as the language filter, in a new column.
  add column if not exists schools text[] not null default '{}',
  add column if not exists degrees text[] not null default '{}',
  add column if not exists fields_of_study text[] not null default '{}',

  -- ISO-3166 alpha-2, or NULL. This is the field is_target_geo already uses to
  -- tell the US San Francisco from the San Franciscos in MX/AR/CR (~2% of
  -- "San Francisco" rows in the export). Present on 100% of the LinkedIn
  -- sample and NULL for every GitHub row -- GitHub's profile location is free
  -- text with no country, and "Cambridge" alone is UK or US, so guessing would
  -- be worse than unknown. A country predicate must therefore admit NULL by
  -- default or it is a source filter.
  add column if not exists country_code text;

-- location_metro's MEANING changes here, with no DDL: it was
-- `(record["city"] or "").strip()` for LinkedIn and hardcoded NULL for
-- GitHub, and no search path ever read it (the metro filter went to
-- `location_raw ilike any(array[...])`, a ScalarArrayOp that cannot use a
-- trigram index -- measured 219 ms and 38k buffers, x4 statements per
-- request). After the re-derive it is a CANONICAL METRO KEY for BOTH sources,
-- via foray.vocab.canonical_metro. 001's plain btree ix_signals_metro becomes
-- useful for the first time: equality / = any() on a canonical key is exactly
-- what a btree serves. No new index for it here, and the alias vocabulary and
-- the query-side metro predicate are owned elsewhere.

-- ---------------------------------------------------------------- indexes --
--
-- Every index below is PARTIAL on `where search_eligible` (012's column). Two
-- reasons, both concrete. First, it shrinks each index by the ineligible
-- population -- prod carries 24,698 LinkedIn rows with no title, no
-- about-text and no experience entries, every one of which would otherwise
-- occupy GIN posting-list space for arrays that are empty anyway. Second and
-- more important, it ENCODES THE ADMISSION RULE INTO THE ACCESS PATH: a query
-- that omits `search_eligible` cannot use these indexes at all, so the
-- planner's choice makes the omission visible in an EXPLAIN instead of
-- silently returning contentless rows. Admission is one rule, computed in
-- derive, tested everywhere -- including here.

-- GIN for the array facets that the data can actually fill: "ever worked at
-- X" (past_employers), "ever held / currently holds title X" (role_titles,
-- current_titles), "went to X" (schools). GIN because these are containment
-- and overlap predicates (@>, &&) over small arrays, which is the access
-- pattern GIN exists for.
create index if not exists ix_signals_past_employers
  on candidate_signals using gin (past_employers) where search_eligible;
create index if not exists ix_signals_role_titles
  on candidate_signals using gin (role_titles) where search_eligible;
create index if not exists ix_signals_current_titles
  on candidate_signals using gin (current_titles) where search_eligible;
create index if not exists ix_signals_schools
  on candidate_signals using gin (schools) where search_eligible;

-- Deliberately NOT indexed: degrees and fields_of_study. An index earns its
-- write cost from selectivity, and at 6.5% / 5.2% record coverage these are
-- mostly-empty arrays -- a GIN over them would be paid for on every derive of
-- 204,535 rows to serve a predicate that cannot be shipped as a hard filter
-- anyway (see the coverage note above). Add them when a richer export moves
-- the numbers, not before.

-- btree for the two ordinals: the whole point of a smallint ladder is that
-- `seniority_rank >= 4` is a RANGE scan rather than a regex over title.
-- Ascending order matters not at all here; the predicate is one-sided.
create index if not exists ix_signals_seniority_rank
  on candidate_signals (seniority_rank) where search_eligible;
create index if not exists ix_signals_mgmt_rank
  on candidate_signals (mgmt_rank) where search_eligible;

-- NOT in this migration, on purpose: the trigram GIN indexes on lower(title),
-- lower(employer) and lower(location_raw) that the query-side facet filter
-- needs. Those belong with the _filter_clauses rewrite that uses them (014),
-- because an index whose query shape does not exist yet is an index nobody can
-- prove is the right one.
