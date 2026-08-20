-- Remove the addresses ContactOut invented when its quota ran out.
--
-- What happened: once the trial quota was spent, ContactOut stopped answering
-- 402 or 429 and started answering **HTTP 200** with a complete sample profile
-- -- "Example Person" at a fictional employer, carrying email1@example.com and
-- email2@gmail.com. The adapter had no reason to distrust a 200, so 32
-- candidates were given that identical pair. Five candidates from the same run
-- got genuine addresses, which is why this deletes by pattern rather than
-- dropping everything the provider returned.
--
-- The freemail half is the one that mattered. example.com is reserved by RFC
-- 2606 and reaches nobody, but email2@gmail.com is syntactically perfect, sorts
-- ahead of work addresses, and would have become the send address. No message
-- went out -- checked before writing this -- but only because nobody enrolled
-- them in time.
--
-- **Scoped to source like 'contactout%' on purpose.** The first draft of this
-- matched any filler local part on any source, and a dry run against production
-- showed it deleting fifteen real addresses -- mail@danijar.com, email@jamie.sh,
-- mail@taeuk.me and friends, all mined from git history, all genuine. Somebody's
-- own domain is exactly where mail@ is a real choice. This incident is one
-- provider's bad run, so the fix is scoped to that provider.
--
-- The guard against a repeat lives in code, not here: enrich/contacts.py refuses
-- placeholder addresses at the last gate before a write, and
-- enrich/contactout.py raises on a sample payload rather than returning an empty
-- result -- an empty result would have stamped the negative cache and made all
-- 32 permanently unenrichable.

create temporary table _placeholder_emails as
select e.id, e.candidate_id
  from candidate_emails e
 where e.source like 'contactout%'
   and (
        -- Reserved by RFC 2606 / 6761: these reach nobody, by design.
        split_part(e.email, '@', 2) in
          ('example.com', 'example.net', 'example.org', 'invalid', 'localhost', 'test')
        or split_part(e.email, '@', 2) ~* '\.(example|invalid|localhost|test)$'
        -- A numbered filler local part: what a generated sample looks like, and
        -- what a person essentially never chooses.
        or split_part(e.email, '@', 1) ~* '^(e?-?mail|test|sample|user|username|name|foo|bar|baz)[-_.]?[0-9]{1,3}$'
        -- Unambiguous filler, whatever the domain.
        or split_part(e.email, '@', 1) ~* '^(sample|example|your[-_.]?e?-?mail|first[-_.]?last|no-?reply|do-?not-?reply)$'
   );

delete from candidate_emails where id in (select id from _placeholder_emails);

-- Un-stamp the negative cache for anyone whose recorded answer was fiction, so a
-- later click can ask again for real. Without this they stay permanently
-- unenrichable on the strength of a payload about "Example Person".
update candidates
   set contactout_checked_at = null
 where id in (select distinct candidate_id from _placeholder_emails);

drop table _placeholder_emails;
