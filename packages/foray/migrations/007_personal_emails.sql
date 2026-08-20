-- Personal-email enrichment: a second paid provider, and a cache that stops us
-- paying twice for the same answer.
--
-- Why a second provider at all: Apollo is a B2B work-contact database. Measured
-- against ten top-scored LinkedIn-only engineers it found a personal address for
-- one of them (10%), and its partner waterfall found none. ContactOut, keyed on
-- the LinkedIn URL we already store, found seven (70%). Corporate addresses are
-- not usable for recruiting outreach, so that difference is the whole feature.
--
-- contactout_checked_at is stamped on a hit AND a miss, the same contract as
-- commit_emails_checked_at in 003. It has to be, because shortlist_events -- the
-- only record of an Apollo spend -- has no uniqueness and is never read as an
-- "already attempted" check, so two clicks on the same candidate means two
-- credits. A negative cache is the only thing that makes a button safe to click
-- twice.

alter table candidate_emails drop constraint candidate_emails_source_check;
alter table candidate_emails add constraint candidate_emails_source_check
  check (source in ('profile', 'commit', 'apollo_work', 'apollo_personal',
                    'guess', 'linkedin', 'contactout_personal', 'contactout_work'));

-- Negative cache + resume cursor. Stamped whether or not an address was found,
-- so a repeated click never re-spends a credit.
alter table candidates add column if not exists contactout_checked_at timestamptz;

-- The work queue selects on this, so index it the way the queue reads it:
-- unchecked first, oldest check next.
create index if not exists ix_candidates_contactout_checked
  on candidates (contactout_checked_at nulls first);
