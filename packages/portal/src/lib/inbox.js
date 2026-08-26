/**
 * The employer inbox, as the portal thinks about it.
 *
 * Pure functions only, so the parts worth being sure about — which chip is
 * shown, what a chip counts, how a sender is named — are testable without a
 * browser. The server classifies; this file only presents.
 */

/**
 * The filters, in the order they appear. `all` is always first and always
 * shown; the rest earn their place by having something in them, because a
 * row of chips that all read zero is a row of dead controls.
 */
export const KINDS = [
  { key: 'all', label: 'All', tone: '' },
  { key: 'verification', label: 'Verification', tone: 'info' },
  { key: 'rejected', label: 'Rejection', tone: 'bad' },
  { key: 'interview', label: 'Interview', tone: 'good' },
  { key: 'assessment', label: 'Assessment', tone: 'warn' },
];

export function chipsFor(counts = {}) {
  return KINDS.filter((kind) => kind.key === 'all' || (counts[kind.key] || 0) > 0).map((kind) => ({
    ...kind,
    count: counts[kind.key] || 0,
  }));
}

/**
 * Who this is from, in as few characters as will still identify them.
 *
 * ATS mail is almost always sent by a no-reply robot with a display name like
 * "Greenhouse Recruiting", which says which vendor and not which employer. So
 * the company on the application wins when we matched one, and the display
 * name is the fallback rather than the first choice.
 */
export function senderLabel(message = {}) {
  if (message.company) return message.company;
  if (message.from_name) return message.from_name;
  const domain = String(message.from_address || '').split('@')[1] || '';
  return domain || message.from_address || 'Unknown sender';
}

/** Two letters for the avatar, from whatever we ended up calling them. */
export function senderInitials(message = {}) {
  const label = senderLabel(message);
  const words = String(label)
    .replace(/[^A-Za-z0-9 ]/g, ' ')
    .split(' ')
    .filter(Boolean);
  if (!words.length) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

/**
 * A one-line summary of what this message means, for people who do not read
 * subject lines. Deliberately not a status: `classify` labels a message, and
 * inferring a pursuit's state from one phrase is the false positive the
 * server-side classifier is written to avoid.
 */
export function kindLabel(kind) {
  switch (kind) {
    case 'verification':
      return 'Verify your account';
    case 'otp':
      return 'One-time code';
    case 'rejected':
      return 'Not moving forward';
    case 'interview':
      return 'They want to talk';
    case 'assessment':
      return 'Assessment to complete';
    case 'acknowledged':
      return 'Application received';
    default:
      return '';
  }
}

/** Newest first, unread ahead of read at the same minute. */
export function order(messages = []) {
  return [...messages].sort((a, b) => {
    const at = Date.parse(a.received_at || 0) || 0;
    const bt = Date.parse(b.received_at || 0) || 0;
    if (bt !== at) return bt - at;
    if (a.read !== b.read) return a.read ? 1 : -1;
    return 0;
  });
}
