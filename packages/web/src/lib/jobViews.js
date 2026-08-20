/**
 * Jobs page views. A query param rather than its own route, so
 * /jobs/:postingId cannot swallow it and App.jsx stays a flat list.
 */

export const JOB_VIEWS = [
  {
    id: 'overview',
    label: 'Overview',
    blurb: 'Every role we know about — surfaced, imported, or entered by hand.',
    panel: 'overview',
  },
  {
    id: 'add',
    label: 'Add',
    blurb: 'Paste a role someone sent us, or import a CSV.',
    panel: 'add',
  },
  {
    id: 'discover',
    label: 'Discover',
    blurb: 'Describe the roles you want. We search the open market and you keep what fits.',
    panel: 'discover',
  },
  {
    id: 'rematch',
    label: 'Rematch',
    blurb: 'People who declined a role, with better-fitting jobs still open.',
    panel: 'rematch',
  },
];

export const DEFAULT_VIEW_ID = 'overview';

export function viewById(id) {
  return JOB_VIEWS.find((view) => view.id === id) ?? JOB_VIEWS[0];
}
