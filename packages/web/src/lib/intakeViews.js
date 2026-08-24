/**
 * Intake page views. A query param rather than its own route, so
 * /intake stays a flat list in App.jsx.
 */

export const INTAKE_VIEWS = [
  {
    id: 'calls',
    label: 'Calls',
    blurb: 'Every voice intake call — expand a row for the transcript and summary.',
    panel: 'calls',
  },
  {
    id: 'text',
    label: 'Text',
    blurb: 'Every messaging intake conversation — expand a row for the full message log.',
    panel: 'text',
  },
  {
    id: 'review',
    label: 'Review queue',
    blurb: 'Calls and texts a recruiter still needs to check or attribute.',
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
  // Older links used ?view=threads.
  const key = id === 'threads' ? 'text' : id;
  return INTAKE_VIEWS.find((view) => view.id === key) ?? INTAKE_VIEWS[0];
}
