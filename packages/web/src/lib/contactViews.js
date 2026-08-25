/**
 * The Contacts grid's preset views.
 *
 * Hardcoded presets rather than user-saved filters: saved views need an
 * entity, a migration, and rename/delete/share semantics, and six presets
 * cover the actual questions people ask of this data.
 *
 * `resolveParams` is a pure function of (view, now) so the date boundaries —
 * which decide who is "going cold" and who has simply not answered yet — are
 * unit-testable rather than a thing you discover is wrong in production.
 */

export const DEFAULT_STALE_DAYS = 90;
// Matches NO_REPLY_GRACE_DAYS on the API. Someone emailed on Monday has not
// failed to reply by Wednesday.
export const NO_REPLY_GRACE_DAYS = 7;
export const RECENTLY_ADDED_DAYS = 14;

export const PAGE_SIZE = 100;
// Mirrors MAX_SELECT_ALL on the API. Above this, "select all matching"
// stops being a selection and becomes a bulk job with no progress to show.
export const MAX_SELECT_ALL = 1000;

/** Column definitions, so each view can show a different subset. */
export const CONTACT_COLUMNS = {
  // Wider than the rest: it carries the avatar and the address underneath.
  full_name: { label: 'Name', width: 260, sort: 'name' },
  current_title: { label: 'Title', width: 200, sort: null },
  current_company: { label: 'Company', width: 170, sort: null },
  last_emailed_at: { label: 'Last contacted', width: 130, sort: 'last_contacted' },
  last_reply_at: { label: 'Last reply', width: 120, sort: 'last_reply' },
  touches: { label: 'Touches', width: 90, sort: 'touches', numeric: true },
  status: { label: 'Status', width: 120, sort: null },
  tags: { label: 'Tags', width: 160, sort: null },
  emails: { label: 'Email', width: 210, sort: null },
  // Intake gave us numbers for people the CRM only had addresses for, and a
  // number nobody can see is a number nobody rings.
  phones: { label: 'Phone', width: 150, sort: null },
  created_at: { label: 'Added', width: 110, sort: 'created' },
  intake_timeline: { label: 'Timeline', width: 130, sort: null },
  intake_target_comp: { label: 'Target comp', width: 120, sort: null, numeric: true },
  intake_match_count: { label: 'Matches', width: 90, sort: null, numeric: true },
  intake_confirmed: { label: 'Confirmed', width: 110, sort: null },
};

const DEFAULT_COLUMNS = [
  'full_name',
  'current_title',
  'current_company',
  'phones',
  'last_emailed_at',
  'last_reply_at',
  'touches',
  // A CRM row without its status is just a mailing list.
  'status',
];

export const CONTACT_VIEWS = [
  {
    id: 'crm',
    label: 'Contacts',
    blurb: 'Everyone you have emailed, shortlisted, or put in a sequence.',
    columns: DEFAULT_COLUMNS,
    sort: 'name',
    dir: 'asc',
    params: { scope: 'crm' },
    empty: 'No one in the CRM yet. Add a contact, or sweep a mailbox from Settings.',
  },
  {
    id: 'going_cold',
    label: 'Going cold',
    blurb: 'Contacted, then quiet on both sides for 90 days.',
    columns: DEFAULT_COLUMNS,
    // Coldest first: the point of the view is the top of the list.
    sort: 'last_contacted',
    dir: 'asc',
    params: { scope: 'crm', relationship: 'stale', stale_days: DEFAULT_STALE_DAYS },
    empty:
      'Nobody here has been quiet for 90 days. Contacts with a reply or a recent ' +
      'send are not counted as cold.',
  },
  {
    id: 'no_reply',
    label: 'No reply',
    blurb: 'Emailed at least once, never answered.',
    columns: DEFAULT_COLUMNS,
    sort: 'last_contacted',
    dir: 'asc',
    params: { scope: 'crm', relationship: 'never_replied' },
    empty:
      'Nobody is waiting on a reply. People emailed in the last 7 days are held ' +
      'back here until they have had a chance to answer.',
  },
  {
    id: 'replied',
    label: 'Replied',
    blurb: 'People who wrote back.',
    columns: DEFAULT_COLUMNS,
    sort: 'last_reply',
    dir: 'desc',
    params: { scope: 'crm', relationship: 'replied' },
    empty: 'Nobody has replied yet.',
  },
  {
    id: 'recent',
    label: 'Recently added',
    blurb: `Added in the last ${RECENTLY_ADDED_DAYS} days.`,
    columns: ['full_name', 'current_title', 'current_company', 'emails', 'created_at'],
    sort: 'created',
    dir: 'desc',
    params: { scope: 'crm' },
    // A bounded window, not just the table re-sorted.
    recentDays: RECENTLY_ADDED_DAYS,
    empty: `Nobody added in the last ${RECENTLY_ADDED_DAYS} days.`,
  },
  {
    id: 'duplicates',
    label: 'Possible duplicates',
    blurb: 'People who may be in the CRM twice.',
    // Its own panel, like the tray: groups of records rather than rows, and
    // its own actions.
    panel: 'duplicates',
    columns: [],
    sort: 'name',
    dir: 'asc',
    params: {},
    empty: 'Nobody looks duplicated.',
  },
  {
    id: 'discovered',
    label: 'Discovered',
    blurb: 'People you have emailed who are not in Contacts yet.',
    // Rendered by DiscoveredTray, not the grid: different rows, different
    // actions. A query param rather than its own route, so /contacts/:id
    // cannot swallow it and App.jsx stays untouched.
    panel: 'discovered',
    columns: [],
    sort: 'name',
    dir: 'asc',
    params: {},
    empty: 'Nothing to review yet.',
  },
  {
    id: 'voice_intake',
    label: 'Intake',
    blurb: 'People who completed a call or text intake, with the preferences they stated.',
    columns: [
      'full_name',
      'current_title',
      // The intake views are where a phone matters most: these are people who
      // rang or texted, and the number is how a recruiter follows up.
      'phones',
      'intake_timeline',
      'intake_target_comp',
      'intake_match_count',
      'intake_confirmed',
    ],
    sort: 'created',
    dir: 'desc',
    params: { scope: 'voice_intake' },
    empty: 'Nobody has completed an intake yet.',
  },
  {
    id: 'all',
    label: 'Sourced pool',
    blurb: 'Every profile in the workspace, contacted or not.',
    columns: ['full_name', 'current_title', 'current_company', 'emails', 'created_at'],
    sort: 'name',
    dir: 'asc',
    params: { scope: 'all' },
    empty: 'No candidates yet — import some from Sourcing.',
  },
];

export const DEFAULT_VIEW_ID = 'crm';

export function viewById(id) {
  return CONTACT_VIEWS.find((view) => view.id === id) ?? CONTACT_VIEWS[0];
}

function daysAgo(now, days) {
  return new Date(now.getTime() - days * 86400000).toISOString();
}

/**
 * Turn a view (plus the live search/sort/page state) into API query params.
 *
 * Pure and clock-injected: every relative window resolves here, once, rather
 * than being recomputed at three call sites with three different ideas of
 * "90 days".
 */
export function resolveParams(viewId, { q = '', sort, dir, page = 0, now = new Date() } = {}) {
  const view = viewById(viewId);
  const params = {
    ...view.params,
    sort: sort || view.sort,
    dir: dir || view.dir,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  };

  if (q) params.q = q;

  if (view.id === 'no_reply') {
    // The API applies the same grace period; sending it explicitly keeps the
    // boundary visible in the URL rather than hidden in a server default.
    params.contacted_before = daysAgo(now, NO_REPLY_GRACE_DAYS);
  }
  if (view.recentDays) {
    params.created_after = daysAgo(now, view.recentDays);
  }
  return params;
}

/** The same params minus paging, for the count call. */
export function countParams(viewId, options = {}) {
  const { limit, offset, sort, dir, ...rest } = resolveParams(viewId, options);
  return rest;
}
