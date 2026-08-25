/**
 * Making `review_reason` readable.
 *
 * The server writes one semicolon-joined audit line, and it is the right thing
 * for the server to write — it is evidence, it fits in 400 characters, and the
 * portal and the messaging side read the same string. What it is not is
 * something a recruiter can act on:
 *
 *   low confidence on active_processes, compensation, ok_to_share_with_clients,
 *   timeline, urgency_driver, work_authorization; required fields missing:
 *   spec.seniority, spec.locations, ...; no explicit permission to share with
 *   clients; caller not identified
 *
 * Two separate problems: field *keys* instead of questions, and four unrelated
 * concerns run together as one sentence. This splits the line back into its
 * clauses and names each field the way the agent asked about it — the labels
 * mirror `intake/schema.py`'s FIELDS, which is the vocabulary the call itself
 * used.
 *
 * Anything this file does not recognise is passed through verbatim. A reason
 * we failed to parse must still be readable, because it is the only account
 * of why a record is being held back.
 */

// Mirrors intake/schema.py FIELDS. Phrased as the thing we do not know, so a
// list of them reads as a list of gaps rather than a list of column names.
const FIELD_LABELS = {
  'spec.job_titles': 'which roles they want',
  'spec.seniority': 'what level they want',
  'spec.locations': 'where they can work',
  'spec.remote_ok': 'remote, hybrid or onsite',
  'spec.must_have_skills': 'must-have skills',
  'spec.nice_to_have_skills': 'nice-to-have skills',
  'spec.industries': 'industries',
  'spec.company_stages': 'company stage',
  'spec.company_sizes': 'company size',
  'spec.target_companies': 'companies they want',
  'spec.excluded_companies': 'companies to avoid',
  'spec.freeform_criteria': 'dealbreakers',
  compensation: 'pay expectations',
  work_authorization: 'work authorisation',
  timeline: 'how soon they can move',
  employment_status: 'employed or searching',
  active_processes: 'other processes they are in',
  ok_to_share_with_clients: 'permission to share them',
  highlights: 'work they are proudest of',
  reason_for_looking: 'why they are looking',
  offers_in_hand: 'offers in hand',
  willing_to_relocate: 'whether they would relocate',
  equity_importance: 'how they weigh equity',
  urgency_driver: 'why now',
  confidential_items: 'what must stay off a writeup',
};

/**
 * The clauses `_review_verdict` and `_join_reasons` can produce, each turned
 * into a titled group. `fields: true` means the tail is a comma-separated key
 * list to relabel.
 */
const CLAUSES = [
  {
    match: /^low confidence on (.+)$/,
    title: 'Worth double-checking',
    tone: 'partial',
    fields: true,
  },
  {
    match: /^required fields missing: (.+)$/,
    title: 'Never covered',
    tone: 'miss',
    fields: true,
  },
  {
    match: /^heuristic extraction: no model was available$/,
    title: 'Read by keyword, not by a model',
    tone: 'partial',
    note: 'Every field on this record is a guess from the transcript text.',
  },
  {
    match: /^no explicit permission to share with clients$/,
    title: 'Cannot be shown to a client',
    tone: 'miss',
    note: 'They never said we may represent them. Ask before this record goes anywhere.',
  },
  {
    match: /^sharing permission read from the transcript, not yet confirmed$/,
    title: 'Sharing permission needs confirming',
    tone: 'partial',
    note: 'The model read a yes out of the transcript. A person has to vouch for it.',
  },
  {
    match: /^caller not identified$/,
    title: 'We do not know who called',
    tone: 'miss',
    note: 'Nothing is written to a candidate record until somebody attributes this.',
  },
  {
    match: /^core-mode trim; deferred fields unasked$/,
    title: 'Short call, by request',
    tone: '',
    note: 'They gave us a few minutes, so the optional questions were skipped.',
  },
  {
    match: /^newer call (\S+) disagrees with a confirmed record$/,
    title: 'Contradicts a record somebody confirmed',
    tone: 'miss',
    note: 'A human vouched for the old answer. Decide which one is right.',
  },
  {
    match: /^thread (\S+) changed a confirmed record$/,
    title: 'Contradicts a record somebody confirmed',
    tone: 'miss',
    note: 'A human vouched for the old answer. Decide which one is right.',
  },
];

export function fieldLabel(key) {
  return FIELD_LABELS[key] ?? key.replace(/^spec\./, '').replace(/_/g, ' ');
}

/**
 * `review_reason` -> `[{title, tone, note, fields}]`, in the order the server
 * wrote them. An empty or absent reason gives an empty list.
 */
export function parseReviewReason(reason) {
  if (!reason || !reason.trim()) return [];
  return reason
    .split(';')
    .map((clause) => clause.trim())
    .filter(Boolean)
    .map((clause) => {
      for (const spec of CLAUSES) {
        const hit = clause.match(spec.match);
        if (!hit) continue;
        return {
          title: spec.title,
          tone: spec.tone,
          note: spec.note ?? '',
          fields: spec.fields
            ? hit[1]
                .split(',')
                .map((key) => key.trim())
                .filter(Boolean)
                .map(fieldLabel)
            : [],
        };
      }
      // Unrecognised: show it as written rather than dropping it.
      return { title: clause, tone: '', note: '', fields: [] };
    });
}
