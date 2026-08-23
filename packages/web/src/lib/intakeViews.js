/**
 * Intake page views. A query param rather than its own route, so
 * /intake stays a flat list in App.jsx.
 */

export const INTAKE_VIEWS = [
  {
    id: 'calls',
    label: 'Calls',
    blurb: 'Every intake call, identified or still waiting on a human.',
    panel: 'calls',
  },
  {
    id: 'review',
    label: 'Review queue',
    blurb: 'Extractions a recruiter still needs to check or attribute.',
    panel: 'review',
  },
  {
    id: 'matches',
    label: 'Job matches',
    blurb: 'Intake candidates ranked against the jobs database.',
    panel: 'matches',
  },
];

export const DEFAULT_VIEW_ID = 'calls';

export function viewById(id) {
  return INTAKE_VIEWS.find((view) => view.id === id) ?? INTAKE_VIEWS[0];
}
