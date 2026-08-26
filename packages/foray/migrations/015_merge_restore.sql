-- 015: make a merge revertible in fact, not just in intention.
--
-- 005 promised "nothing is deleted ... so a bad merge reverts cleanly", but
-- revert_merge was rebuilding the pre-merge state from guesses: it forced the
-- winner's source back to 'github' (wrong for a row that was already 'both'),
-- moved back every raw_profile and every source='linkedin' address regardless
-- of which ones the merge had actually moved, and never undid the identity
-- columns apply_merge had written. A human rejecting a merge got a third state
-- that was neither before nor after.
--
-- The fix is to stop guessing: apply_merge records exactly what it changed and
-- revert_merge replays that record. This column is that record. It is jsonb
-- rather than columns because its shape is per-tier and only ever read by the
-- revert that wrote it -- nothing joins on it.
alter table candidate_merges
  add column if not exists restore jsonb not null default '{}'::jsonb;

-- The pipeline's half of the same bug: find_pairs consults only `candidates`,
-- so a reverted pair was rediscovered on the next --apply and silently
-- re-merged. resolve.py now filters on this, and the partial index is the
-- access path for that not-exists.
create index if not exists ix_merges_pair_decided
    on candidate_merges (primary_id, merged_id)
    where status in ('rejected', 'reverted');
