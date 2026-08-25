/**
 * Applications page views. A query param rather than its own route, so
 * /applications stays a flat entry in App.jsx — the same shape /intake and
 * /jobs use.
 *
 * The order is the recruiter's attention order, not the lifecycle's: what
 * needs a human first, then what is in flight, then what is done.
 */

export const APPLICATION_VIEWS = [
  {
    id: 'open',
    label: 'In flight',
    blurb: 'Everything still moving — prepared, awaiting the candidate, or out with an employer.',
    statuses: [
      'draft',
      'prepared',
      'awaiting_consent',
      'submitting',
      'submitted',
      'acknowledged',
      'interviewing',
      'offer',
    ],
  },
  {
    id: 'review',
    label: 'Needs the candidate',
    blurb: 'Packages built and waiting on a yes. Nothing is sent until they approve it.',
    statuses: ['prepared', 'awaiting_consent'],
  },
  {
    id: 'sent',
    label: 'Sent',
    blurb: 'Submitted or acknowledged by the employer.',
    statuses: ['submitted', 'acknowledged'],
  },
  {
    id: 'closed',
    label: 'Closed',
    blurb: 'Rejected or withdrawn.',
    statuses: ['rejected', 'withdrawn'],
  },
  {
    id: 'all',
    label: 'All',
    blurb: 'Every application, whatever its state.',
    statuses: [],
  },
];

export function applicationView(id) {
  return APPLICATION_VIEWS.find((view) => view.id === id) ?? APPLICATION_VIEWS[0];
}

/** Client-side filter: the list endpoint takes one status, these take sets. */
export function inView(view, application) {
  return view.statuses.length === 0 || view.statuses.includes(application.status);
}
