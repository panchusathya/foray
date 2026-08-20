-- 009: whole-graph commit search (Search API author:{login}).
--
-- Own-repo mining (commit_emails_checked_at) only sees commits on the
-- candidate's top repos. Commit search covers every public repo GitHub has
-- indexed, still attributed by GitHub's own author.login resolution.
--
-- Negative cache + resume cursor, same contract as commit_emails_checked_at
-- in 003 and contactout_checked_at in 007: stamped on a hit AND a miss, never
-- on an error. A crash mid-run loses at most the in-flight candidate.

alter table candidates
  add column if not exists commit_search_checked_at timestamptz;

create index if not exists ix_candidates_commit_search_checked
  on candidates (commit_search_checked_at nulls first);
