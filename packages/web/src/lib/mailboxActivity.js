/**
 * Which mailbox activity is still worth a card on the Settings page.
 *
 * An import or a sweep writes a terminal status of "done" and nothing ever
 * writes it back to "idle", so the progress card that appears while a run is
 * working used to stay on screen forever: a sweep that finished on 20 August
 * still read as current in late September, next to a mailbox nobody had
 * touched since.
 *
 * The state itself is worth keeping -- started_at and finished_at are the only
 * record that the run happened -- so this hides the card rather than clearing
 * the state, and it does it in the browser rather than making the API lie
 * about what it knows.
 *
 * Three rules, in the order a recruiter cares about them. Anything in flight
 * is always shown. So is a failure, however old, because that is the one a
 * person still has to act on. A success is shown for a day: long enough to see
 * the result of something you kicked off, short enough that it stops being
 * furniture.
 */

export const ACTIVITY_WINDOW_HOURS = 24;

const IN_FLIGHT = new Set(['queued', 'running']);

/** Is this half of a mailbox's activity (the import, or the sweep) still current? */
export function activityVisible(part, now = new Date()) {
  const status = part?.status || 'idle';
  if (status === 'idle') return false;
  if (IN_FLIGHT.has(status)) return true;
  // A failure stays up until somebody deals with it. `error` is checked too
  // because discovery's fail path sets the message before the status.
  if (status === 'failed' || part?.error) return true;
  // An unfamiliar status is shown rather than swallowed: a new state added on
  // the server should be visible here before anyone thinks to update this file.
  if (status !== 'done') return true;
  const finished = Date.parse(part?.finished_at ?? '');
  // "done" with no finishing time is the shape the old rows have, and it is
  // exactly the case that used to linger. Treat it as long over.
  if (Number.isNaN(finished)) return false;
  return now.getTime() - finished < ACTIVITY_WINDOW_HOURS * 3600 * 1000;
}

/** Does this mailbox have anything worth showing at all? */
export function rowVisible(row, now = new Date()) {
  return activityVisible(row?.backfill, now) || activityVisible(row?.discovery, now);
}
