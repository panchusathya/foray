/**
 * Labels and formatting for the Deliverability page.
 *
 * Kept out of the component so the words a health label maps to live in one
 * place: "in spam" is a strong claim, and the page should make it only when
 * the server's label says so.
 */

export const HEALTH = {
  in_spam: { text: 'In spam', tone: 'miss' },
  at_risk: { text: 'At risk', tone: 'partial' },
  healthy: { text: 'Healthy', tone: 'strong' },
  no_data: { text: 'Not enough data', tone: '' },
  mixed: { text: 'Mixed', tone: '' },
};

export const PLACEMENT = {
  inbox: { text: 'Inbox', tone: 'strong' },
  other_tab: { text: 'Other tab', tone: 'partial' },
  spam: { text: 'Spam', tone: 'miss' },
  missing: { text: 'Missing', tone: 'miss' },
  sent: { text: 'Looking…', tone: '' },
  queued: { text: 'Queued', tone: '' },
  failed: { text: 'Not sent', tone: '' },
};

export const INTERVALS = [
  { hours: 0, label: 'Off (Test now only)' },
  { hours: 12, label: 'Every 12 hours' },
  { hours: 24, label: 'Daily' },
  { hours: 48, label: 'Every 2 days' },
  { hours: 168, label: 'Weekly' },
];

export const SEED_PROVIDERS = [
  { value: 'gmail', label: 'Gmail', help: 'Turn on 2-Step Verification, then make an app password at myaccount.google.com/apppasswords. New account with no app passwords? Use Connect with Google above.' },
  { value: 'yahoo', label: 'Yahoo', help: 'Account security → Generate app password, or use Connect with Yahoo above.' },
  { value: 'other', label: 'Other (IMAP)', help: 'Any inbox that offers IMAP. Outlook.com is not supported yet.' },
];

export function pct(value, digits = 1) {
  if (value === null || value === undefined) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

/** "3 of 4 in inbox" for a {inbox, other_tab, spam, missing, pending} count. */
export function seedSummary(seed) {
  if (!seed) return '—';
  const results = (seed.inbox ?? 0) + (seed.other_tab ?? 0) + (seed.spam ?? 0) + (seed.missing ?? 0);
  if (!results) return seed.pending ? `${seed.pending} waiting` : 'No tests yet';
  return `${seed.inbox ?? 0} of ${results} in inbox`;
}

/** Inbox share of seed results, or null when there are none. */
export function inboxRate(seed) {
  if (!seed) return null;
  const results = (seed.inbox ?? 0) + (seed.other_tab ?? 0) + (seed.spam ?? 0) + (seed.missing ?? 0);
  return results ? (seed.inbox ?? 0) / results : null;
}

export function when(value) {
  if (!value) return '';
  return new Date(value).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

export const CHECK_TONE = {
  ok: 'strong',
  clean: 'strong',
  weak: 'partial',
  unknown: '',
  missing: 'miss',
  invalid: 'miss',
  listed: 'miss',
};
