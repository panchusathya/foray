// Maps a Foray search hit to a Juicebox CandidateIn payload. Pure and
// dependency-free so it can be unit-tested without a browser.
//
// source_id = the GitHub login gives free idempotent dedupe on
// (org_id, source, source_id): re-importing the same person updates rather
// than duplicates. Guessed emails never reach here -- the Foray search API
// filters source='guess' in SQL -- so hit.emails is safe to pass straight to
// Candidate.emails (and thus primary_email for the sequencer).

export function forayHitToCandidate(hit, { archetypeId = null } = {}) {
  const emails = (hit.emails ?? []).map((e) => e.email).filter(Boolean);
  return {
    source: 'foray',
    source_id: hit.login,
    full_name: hit.name || hit.login, // required; empty name is rejected server-side
    github_url: hit.github_url || '',
    linkedin_url: hit.linkedin_url || '',
    location: hit.location || '',
    current_company: hit.employer || '',
    current_company_domain: hit.employer_domain || '',
    years_experience: hit.est_experience_years ?? null,
    skills: [hit.primary_language, ...(hit.domain_markers ?? []), ...(hit.topics ?? [])]
      .filter(Boolean),
    emails,
    headline:
      hit.employer && hit.primary_language
        ? `${hit.primary_language} engineer at ${hit.employer}`
        : hit.primary_language
          ? `${hit.primary_language} engineer`
          : '',
    raw: {
      foray: {
        score: hit.score,
        highlights: hit.highlights,
        flags: hit.flags,
        inferred: hit.inferred,
        top_repos: hit.top_repos,
        last_push_at: hit.last_push_at,
        experience_confidence: hit.experience_confidence,
        email_sources: (hit.emails ?? []).map((e) => e.source),
        archetype: archetypeId,
      },
    },
  };
}

// Split a selection into importable vs email-less, so the UI can warn: the
// sequencer permanently kills an enrollment for a candidate with no email.
export function partitionByEmail(hits) {
  const withEmail = [];
  const withoutEmail = [];
  for (const hit of hits) {
    ((hit.emails ?? []).some((e) => e.email) ? withEmail : withoutEmail).push(hit);
  }
  return { withEmail, withoutEmail };
}

// Fold a find-emails response back into the selected hits, so the button label,
// the import warning and the contact chips all agree without waiting for a
// refetch. Pure, for the same reason as everything else in this file.
//
// Personal addresses go first, ahead of anything already on the hit. That is not
// cosmetic: emails[0] becomes Candidate.primary_email and is the address the
// sequencer sends to, so a work address in front of a personal one puts a
// recruiting pitch in somebody's work inbox.
export function mergeFoundEmails(hits, results = {}) {
  return hits.map((hit) => {
    const outcome = results[hit.login];
    if (!outcome) return hit;
    const found = [
      ...(outcome.personal ?? []).map((email) => ({
        email,
        source: 'contactout_personal',
        is_work: false,
        verified: false,
      })),
      ...(outcome.work ?? []).map((email) => ({
        email,
        source: 'contactout_work',
        is_work: true,
        verified: false,
      })),
    ];
    if (!found.length) return hit;
    const already = new Set((hit.emails ?? []).map((e) => e.email));
    const fresh = found.filter((e) => !already.has(e.email));
    if (!fresh.length) return hit;
    return { ...hit, emails: [...fresh, ...(hit.emails ?? [])] };
  });
}

// What to tell the user after a lookup. Every count here is a decision the
// server made about whether to spend money, so say which -- "nothing found" and
// "we already had one" look identical otherwise.
export function describeFindEmails(summary = {}, results = {}) {
  const statuses = Object.values(results).map((r) => r.status);
  const count = (name) => statuses.filter((s) => s === name).length;
  const parts = [];
  const found = summary.found_personal ?? count('found');
  parts.push(found === 1 ? '1 personal address found' : `${found} personal addresses found`);
  const none = count('none');
  if (none) parts.push(`${none} with nothing on file`);
  const covered = count('has_personal');
  if (covered) parts.push(`${covered} already had one`);
  const cached = count('cached');
  if (cached) parts.push(`${cached} looked up before`);
  const noProfile = count('no_profile');
  if (noProfile) parts.push(`${noProfile} with no LinkedIn profile to match`);
  const unknown = count('unknown');
  if (unknown) parts.push(`${unknown} not in the sourcing database`);
  const errors = count('error');
  if (errors) parts.push(`${errors} failed — try those again`);
  return `${parts.join(', ')}.`;
}
