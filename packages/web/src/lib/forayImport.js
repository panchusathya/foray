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
