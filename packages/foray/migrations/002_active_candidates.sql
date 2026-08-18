-- The active subset: candidates a human has shortlisted, or who score in the top
-- decile of any archetype. Refreshed nightly. The volatile/event refresh tiers
-- only touch this ~small set, not the whole pool.
create materialized view if not exists active_candidates as
select c.id, c.login
from candidates c
where c.id in (select candidate_id from shortlist_events)
   or c.id in (
        select candidate_id
        from (
            select candidate_id,
                   percent_rank() over (partition by archetype_id order by score desc) as pr
            from archetype_scores
        ) ranked
        where ranked.pr <= 0.1
   );

-- Unique index enables REFRESH MATERIALIZED VIEW CONCURRENTLY.
create unique index if not exists ix_active_candidates_id on active_candidates (id);
