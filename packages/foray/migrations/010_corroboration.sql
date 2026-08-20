-- 010: free corroboration scoring for candidate_emails.
--
-- candidate_emails.verified is documented as settable only by a provider that
-- asserts deliverability, and nothing does. These columns are the free
-- substitute: independent-source count, last evidence, a Gravatar existence
-- probe, and a 0-100 reach_score derived from those. A Gravatar miss must
-- never suppress an address -- plenty of real developers have no Gravatar.
--
-- reach_score is a send-floor, not a re-rank. Search still orders by
-- is_work asc, confidence desc; the sequencer refuses to send below
-- FORAY_MIN_REACH_SCORE.

alter table candidate_emails
  add column if not exists source_count smallint not null default 1
      check (source_count >= 0),
  add column if not exists last_evidence_at timestamptz,
  add column if not exists gravatar_state text
      check (gravatar_state in ('hit', 'miss', 'unknown')),
  add column if not exists gravatar_checked_at timestamptz,
  add column if not exists reach_score smallint
      check (reach_score is null or reach_score between 0 and 100);

create index if not exists ix_candidate_emails_unscored
  on candidate_emails (id)
  where reach_score is null and source <> 'guess';
