/**
 * What an application *means*, with no React in sight.
 *
 * The server's status vocabulary is a state machine written for the recruiter
 * side; a candidate reading it needs three different things from the same
 * word — a label they understand, a colour that tells them whether it is on
 * them, and a position in a pipeline. All three live here so the tracker, the
 * home screen and the detail view can never disagree about them, and so the
 * mapping can be unit-tested (test/applications.test.js).
 */

// The candidate's words, not the state machine's.
const LABELS = {
  draft: 'Preparing',
  prepared: 'Ready for you',
  awaiting_consent: 'Needs your OK',
  submitting: 'Sending',
  submitted: 'Applied',
  acknowledged: 'Received',
  interviewing: 'Interviewing',
  offer: 'Offer',
  rejected: 'Closed',
  withdrawn: 'Withdrawn',
};

// needs  = the candidate is the blocker (gold)
// moving = we are doing something right now (purple)
// good   = an outcome worth celebrating (green)
// done   = over (grey)
// ''     = neutral, just where it sits
const TONES = {
  draft: 'moving',
  prepared: 'needs',
  awaiting_consent: 'needs',
  submitting: 'moving',
  submitted: '',
  acknowledged: '',
  interviewing: 'moving',
  offer: 'good',
  rejected: 'done',
  withdrawn: 'done',
};

export const CLOSED = ['rejected', 'withdrawn'];
export const NEEDS_YOU = ['prepared', 'awaiting_consent'];
export const IN_PROCESS = ['interviewing', 'offer'];
export const SENT = ['submitting', 'submitted', 'acknowledged'];

export function statusLabel(status) {
  return LABELS[status] || status || 'Unknown';
}

export function statusTone(status) {
  return TONES[status] ?? '';
}

// Only a genuinely in-flight status gets the pulsing dot. A pulse that never
// stops is decoration; a pulse on "we are sending this right now" is status.
export function isLive(status) {
  return status === 'submitting';
}

export function needsYou(application) {
  return NEEDS_YOU.includes(application?.status);
}

export function isClosed(status) {
  return CLOSED.includes(status);
}

/** The one line that says why this application is sitting in someone's lap. */
export function actionPrompt(application) {
  if (!application) return '';
  if (application.status === 'prepared') return 'Read what we wrote before it goes out';
  if (application.status === 'awaiting_consent') {
    return application.can_auto_submit
      ? 'Approved — send it, or have us send it'
      : "Approved — apply on the employer's site, then mark it sent";
  }
  return '';
}

// ---------------------------------------------------------------- pipeline

/**
 * The tracker's columns, left to right in the order an application travels.
 * `tone` drives the dot colour on the column header.
 */
export const COLUMNS = [
  { key: 'preparing', label: 'Preparing', tone: 'moving', statuses: ['draft'] },
  { key: 'needs', label: 'Needs you', tone: 'needs', statuses: NEEDS_YOU },
  { key: 'applied', label: 'Applied', tone: '', statuses: SENT },
  { key: 'interviewing', label: 'Interviewing', tone: 'moving', statuses: ['interviewing'] },
  { key: 'offer', label: 'Offer', tone: 'good', statuses: ['offer'] },
  { key: 'closed', label: 'Closed', tone: '', statuses: CLOSED },
];

export function columnFor(status) {
  return COLUMNS.find((column) => column.statuses.includes(status)) || null;
}

/** Every column, always — an empty column is information ("nothing yet"). */
export function groupByColumn(applications = []) {
  const groups = new Map(COLUMNS.map((column) => [column.key, []]));
  for (const application of applications) {
    const column = columnFor(application.status);
    if (column) groups.get(column.key).push(application);
  }
  return COLUMNS.map((column) => ({ ...column, items: groups.get(column.key) }));
}

// ------------------------------------------------------------------- track

const STAGES = [
  { key: 'prepared', label: 'Prepared' },
  { key: 'approved', label: 'Approved' },
  { key: 'applied', label: 'Applied' },
  { key: 'interviewing', label: 'Interview' },
  { key: 'offer', label: 'Offer' },
];

const STAGE_INDEX = {
  draft: 0,
  prepared: 0,
  awaiting_consent: 1,
  submitting: 1,
  submitted: 2,
  acknowledged: 2,
  interviewing: 3,
  offer: 4,
};

/**
 * The progress bar on the detail screen, or null when there is nothing
 * honest to draw.
 *
 * Closed applications get null on purpose. A rejection can arrive after an
 * interview or instead of one and the portal is not told which, so any bar we
 * drew would be a guess presented as history.
 */
export function trackFor(status) {
  if (isClosed(status)) return null;
  const reached = STAGE_INDEX[status];
  if (reached === undefined) return null;
  return STAGES.map((stage, index) => ({
    ...stage,
    state: index < reached ? 'done' : index === reached ? 'now' : 'todo',
  }));
}

// -------------------------------------------------------------- summarising

export function summarise(applications = []) {
  const live = applications.filter((a) => !isClosed(a.status));
  return {
    total: applications.length,
    live: live.length,
    needsYou: applications.filter((a) => NEEDS_YOU.includes(a.status)).length,
    sent: applications.filter((a) => SENT.includes(a.status)).length,
    interviewing: applications.filter((a) => a.status === 'interviewing').length,
    offers: applications.filter((a) => a.status === 'offer').length,
    closed: applications.filter((a) => isClosed(a.status)).length,
  };
}

/** Newest movement first — `updated_at` is what changed, not when it started. */
export function byRecency(applications = []) {
  return [...applications].sort(
    (a, b) => Date.parse(b.updated_at || 0) - Date.parse(a.updated_at || 0),
  );
}

export function matchesQuery(application, query) {
  const needle = (query || '').trim().toLowerCase();
  if (!needle) return true;
  return `${application.title || ''} ${application.company || ''} ${application.location || ''}`
    .toLowerCase()
    .includes(needle);
}

// ------------------------------------------------------------------ format

/** Initials for the monogram tiles. Two letters, never punctuation. */
export function initials(name) {
  const words = String(name || '')
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Short relative time. `now` is injectable so the tests are not clock-flaky. */
export function ago(value, now = Date.now()) {
  if (!value) return '';
  const then = Date.parse(value);
  if (Number.isNaN(then)) return '';
  const delta = now - then;
  if (delta < 2 * MINUTE) return 'just now';
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)}m ago`;
  if (delta < DAY) return `${Math.floor(delta / HOUR)}h ago`;
  if (delta < 7 * DAY) return `${Math.floor(delta / DAY)}d ago`;
  if (delta < 30 * DAY) return `${Math.floor(delta / (7 * DAY))}w ago`;
  return new Date(then).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** "Good morning" / "Good afternoon" / "Good evening", from the local hour. */
export function greeting(now = new Date()) {
  const hour = now.getHours();
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}
