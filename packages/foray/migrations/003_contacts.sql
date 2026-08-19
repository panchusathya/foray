-- Contact enrichment. Emails are multi-valued and multi-source (a person can
-- have a public profile address, commit-author addresses, and Apollo work +
-- personal addresses), so they live in a child table -- the same reasoning as
-- archetype_scores in 001 -- rather than wide columns on candidates.

create table if not exists candidate_emails (
    id            bigint generated always as identity primary key,
    candidate_id  bigint not null references candidates(id) on delete cascade,
    email         text   not null,              -- stored lowercased + trimmed
    source        text   not null check (source in
                    ('profile','commit','apollo_work','apollo_personal','guess')),
    is_work       boolean  not null default false,  -- domain not in data/freemail.txt
    confidence    smallint not null default 50 check (confidence between 0 and 100),
    verified      boolean  not null default false,  -- only a verifier may set this
    evidence      text,                             -- provenance, shown on hover
    occurrences   int not null default 1,           -- commit-mining vote count
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    unique (candidate_id, email)
);

create index if not exists ix_candidate_emails_candidate on candidate_emails (candidate_id);
create index if not exists ix_candidate_emails_email     on candidate_emails (email);

-- Negative cache + resume cursor for commit-email mining: stamped whether or
-- not an address was found, so a resumed run never re-spends a REST call.
alter table candidates add column if not exists commit_emails_checked_at timestamptz;

-- The employer's primary domain (from a non-freemail profile email, or Apollo).
-- Feeds the main app's current_company_domain and its email pattern guesser.
alter table candidate_signals add column if not exists employer_domain text;
