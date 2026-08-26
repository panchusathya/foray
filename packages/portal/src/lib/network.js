/**
 * The candidate's network, assembled from per-application contact lists.
 *
 * The API answers "who do we know at the place this application went to"
 * (`GET /applications/{id}/networking`) — there is no cross-application people
 * endpoint. The People tab wants the other shape: one row per human, with
 * every application that person is relevant to hanging off them, so somebody
 * who works at a company you applied to twice is one card and not two.
 *
 * That fold is pure, so it lives here and is unit-tested
 * (test/network.test.js) rather than inside a component that fetches.
 */

/** Rank first by whether we can actually act, then by strength of the tie. */
const BASIS_RANK = { current: 0, former: 1 };

export function basisLabel(basis) {
  if (basis === 'current') return 'Works there';
  if (basis === 'former') return 'Used to work there';
  return basis || '';
}

/**
 * Fold `[{ application, suggestions }]` into one row per person.
 *
 * Contacts are keyed by candidate_id — the same id the draft endpoint takes,
 * so a merged row can still ask for a draft against any of its applications.
 */
export function mergePeople(entries = []) {
  const people = new Map();

  for (const entry of entries) {
    const application = entry?.application;
    if (!application) continue;
    for (const contact of entry.suggestions || []) {
      const id = contact.candidate_id;
      if (!id) continue;

      const existing = people.get(id);
      const at = {
        applicationId: application.id,
        title: application.title || '',
        company: application.company || '',
        status: application.status || '',
      };

      if (!existing) {
        people.set(id, {
          id,
          name: contact.name || 'Contact',
          title: contact.title || '',
          company: contact.company || application.company || '',
          basis: contact.basis || '',
          linkedin_url: contact.linkedin_url || '',
          // Whichever application already has a draft wins; a person with a
          // written message is one tap from being useful.
          draft: contact.draft || '',
          draftFor: contact.draft ? application.id : '',
          at: [at],
        });
        continue;
      }

      existing.at.push(at);
      if (!existing.draft && contact.draft) {
        existing.draft = contact.draft;
        existing.draftFor = application.id;
      }
      if (!existing.title && contact.title) existing.title = contact.title;
      if (!existing.linkedin_url && contact.linkedin_url) {
        existing.linkedin_url = contact.linkedin_url;
      }
      // "Works there" beats "used to" for the same person at the same place.
      if (rank(contact.basis) < rank(existing.basis)) existing.basis = contact.basis;
    }
  }

  return sortPeople([...people.values()]);
}

function rank(basis) {
  return BASIS_RANK[basis] ?? 9;
}

/**
 * Drafted first, then current employees, then alphabetical.
 *
 * Deliberately not "most recent application first": the question this tab
 * answers is "who should I message", and a ready message is the strongest
 * signal that the answer is this person.
 */
export function sortPeople(people = []) {
  return [...people].sort((a, b) => {
    const drafted = Number(Boolean(b.draft)) - Number(Boolean(a.draft));
    if (drafted) return drafted;
    const tie = rank(a.basis) - rank(b.basis);
    if (tie) return tie;
    return String(a.name).localeCompare(String(b.name));
  });
}

/** Companies present in the list, each with a count, for the filter row. */
export function companiesOf(people = []) {
  const counts = new Map();
  for (const person of people) {
    for (const at of person.at) {
      if (!at.company) continue;
      counts.set(at.company, (counts.get(at.company) || 0) + 1);
    }
  }
  return [...counts.entries()]
    .map(([company, count]) => ({ company, count }))
    .sort((a, b) => b.count - a.count || a.company.localeCompare(b.company));
}

export function atCompany(person, company) {
  if (!company) return true;
  return person.at.some((at) => at.company === company);
}

export function matchesQuery(person, query) {
  const needle = (query || '').trim().toLowerCase();
  if (!needle) return true;
  const where = person.at.map((at) => `${at.company} ${at.title}`).join(' ');
  return `${person.name} ${person.title} ${person.company} ${where}`
    .toLowerCase()
    .includes(needle);
}

/**
 * Run `fn` over `items` at most `limit` at a time, preserving order.
 *
 * The People tab fans out one request per application. Twenty applications
 * firing twenty parallel requests at an API that computes contacts on demand
 * is a self-inflicted thundering herd; four at a time still fills the screen
 * quickly. Rejections resolve to null rather than failing the batch — one
 * company we cannot look up must not blank the whole page.
 */
export async function mapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let cursor = 0;

  async function worker() {
    for (;;) {
      const index = cursor++;
      if (index >= items.length) return;
      try {
        results[index] = await fn(items[index], index);
      } catch {
        results[index] = null;
      }
    }
  }

  const size = Math.max(1, Math.min(limit, items.length));
  await Promise.all(Array.from({ length: size }, worker));
  return results;
}
