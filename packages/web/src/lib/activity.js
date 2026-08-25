/**
 * Turning an `/v1/analytics/activity` row into something you can click.
 *
 * The subject of most outreach events is a *message*, which has no page. The
 * API resolves the person for those rows (`candidate_id`), so the rule here is
 * simple: prefer the person, fall back to the page the subject lives on, and
 * leave the row unlinked rather than inventing a destination that 404s.
 */

const EVENT_LABELS = {
  'search.run': 'Search run',
  'agent.run': 'Agent run',
  'outreach.sent': 'Email sent',
  'outreach.opened': 'Email opened',
  'outreach.clicked': 'Link clicked',
  'outreach.replied': 'Reply received',
  'outreach.failed': 'Send failed',
  'application.status_changed': 'Application moved',
  'candidate.created': 'Contact added',
  'candidate.updated': 'Contact updated',
};

// Events worth pulling the eye to. Everything else is background noise.
const NOTABLE = new Set(['outreach.replied', 'outreach.failed']);

export function eventLabel(type) {
  return EVENT_LABELS[type] ?? type;
}

export function eventTone(type) {
  if (type === 'outreach.failed') return 'miss';
  return NOTABLE.has(type) ? 'strong' : 'accent';
}

/** Where this row goes, or null when nothing sensible exists. */
export function eventTarget(event) {
  if (!event) return null;
  if (event.candidate_id) return `/contacts/${event.candidate_id}`;
  switch (event.subject_type) {
    case 'application':
      return `/applications/${event.subject_id}`;
    case 'intake_call':
      return '/intake?view=calls';
    case 'intake_thread':
      return '/intake?view=text';
    case 'sourcing_agent':
      return '/agents';
    case 'mailbox':
      return '/settings';
    default:
      return null;
  }
}

/**
 * "2:14 PM" for today, "Mon 2:14 PM" inside the week, a date beyond that.
 * The full stamp on every row was the thing making the panel unreadable —
 * eight identical-looking strings, none of which you can scan.
 */
export function eventWhen(value, now = new Date()) {
  if (!value) return '';
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return '';
  const time = at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const days = Math.floor((now - at) / 86400000);
  if (at.toDateString() === now.toDateString()) return time;
  if (days < 7) return `${at.toLocaleDateString([], { weekday: 'short' })} ${time}`;
  return at.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
