-- 011: name-bridge merge tier + negative cache.
--
-- LinkedIn-only engineers are joined to GitHub identities by commit-search
-- author-name, then corroborated (slug / employer / metro / rare name). A
-- hit mints a GitHub candidate if needed and queues a review-only merge;
-- it never writes an address onto the LinkedIn row. commit_name is that
-- review tier -- it is not in find_pairs and is never auto-applied.
--
-- name_bridge_checked_at follows the 003/007 contract: stamped on hit and
-- miss, never on an error.

alter table candidate_merges drop constraint if exists candidate_merges_tier_check;
alter table candidate_merges add constraint candidate_merges_tier_check
  check (tier in (
      'linkedin_url', 'business_email', 'name_domain', 'name_metro', 'commit_name'
  ));

alter table candidates
  add column if not exists name_bridge_checked_at timestamptz;

create index if not exists ix_candidates_name_bridge_checked
  on candidates (name_bridge_checked_at nulls first);
