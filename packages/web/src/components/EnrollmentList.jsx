// The sequence editor's list of enrolled people.
//
// A live sequence has hundreds of rows, and the list used to push everything
// below it (and the page's own end) a long scroll away. It now opens as a
// summary -- counts by status, each one a filter -- and the rows sit in their
// own scrolling box with a search. It starts collapsed once there are more
// than COLLAPSE_FROM people; whatever the recruiter last chose is remembered.
import { useMemo, useState } from 'react';

const COLLAPSE_FROM = 10;
const STORAGE_KEY = 'sequence.enrollments.open';

const STATUS_ORDER = ['active', 'replied', 'completed', 'stopped', 'bounced', 'failed'];
const STATUS_TONE = {
  active: 'strong',
  replied: 'accent',
  completed: '',
  stopped: 'partial',
  bounced: 'miss',
  failed: 'miss',
};

function remembered() {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === null ? null : value === '1';
  } catch {
    return null;
  }
}

function remember(open) {
  try {
    window.localStorage.setItem(STORAGE_KEY, open ? '1' : '0');
  } catch {
    // A convenience only.
  }
}

export function EnrollmentList({ enrollments, trailer, canReenrol, onReenrol, reenrollingId }) {
  const [openChoice, setOpenChoice] = useState(remembered);
  const [status, setStatus] = useState('');
  const [query, setQuery] = useState('');
  const open = openChoice ?? enrollments.length <= COLLAPSE_FROM;

  const counts = useMemo(() => {
    const out = {};
    for (const row of enrollments) out[row.status] = (out[row.status] ?? 0) + 1;
    return out;
  }, [enrollments]);
  const statuses = [
    ...STATUS_ORDER.filter((s) => counts[s]),
    ...Object.keys(counts).filter((s) => !STATUS_ORDER.includes(s)),
  ];
  const sentAny = enrollments.filter((row) => row.current_step > 0).length;

  const needle = query.trim().toLowerCase();
  const shown = enrollments.filter((row) => {
    if (status && row.status !== status) return false;
    if (!needle) return true;
    const name = (row.candidate?.full_name ?? row.candidate_id ?? '').toLowerCase();
    return name.includes(needle);
  });

  function toggle(next = !open) {
    setOpenChoice(next);
    remember(next);
  }

  function pick(next) {
    setStatus((current) => (current === next ? '' : next));
    if (!open) toggle(true);
  }

  return (
    <div className="enrol-list" data-testid="enrollment-list">
      <div className="enrol-summary">
        <button
          type="button"
          className="tiny ghost enrol-toggle"
          aria-expanded={open}
          onClick={() => toggle()}
          data-testid="enrollment-toggle"
        >
          {open ? '▾ Hide list' : '▸ Show list'}
        </button>
        <span className="enrol-total">
          <strong>{enrollments.length.toLocaleString()}</strong> enrolled
          <span className="muted"> · {sentAny.toLocaleString()} sent at least one email</span>
        </span>
        <span className="row wrap" style={{ gap: 6 }}>
          {statuses.map((s) => (
            <button
              key={s}
              type="button"
              className={`enrol-chip ${STATUS_TONE[s] ?? ''}${status === s ? ' on' : ''}`}
              aria-pressed={status === s}
              onClick={() => pick(s)}
              title={status === s ? 'Show everyone' : `Show only ${s}`}
            >
              {s} <strong>{counts[s].toLocaleString()}</strong>
            </button>
          ))}
        </span>
      </div>

      {open ? (
        <>
          <div className="row" style={{ gap: 8 }}>
            <input
              type="search"
              placeholder="Search by name…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              style={{ maxWidth: 280 }}
              aria-label="Search enrolled people"
            />
            <span className="muted small">
              {shown.length === enrollments.length
                ? `${shown.length.toLocaleString()} shown`
                : `${shown.length.toLocaleString()} of ${enrollments.length.toLocaleString()} shown`}
            </span>
          </div>
          {/* Two lines per person, not a table: the card sits in the editor's
              narrow column, where five columns wrapped every row to five lines.
              Nothing is cut off -- a long line widens the list and the box
              scrolls sideways as well as down. */}
          <div className="enrol-scroll" data-testid="enrollment-scroll">
          <ul className="enrol-rows">
            {shown.map((enrollment) => (
              <li key={enrollment.id} className="enrol-row" data-testid="enrollment-row">
                <div className="enrol-line">
                  <span className="enrol-name">
                    {enrollment.candidate?.full_name ?? enrollment.candidate_id}
                  </span>
                  {!(enrollment.candidate?.emails ?? []).length ? (
                    <span className="muted small" title="No email on file — this step can't send yet">
                      no email
                    </span>
                  ) : null}
                  <span className="enrol-right">
                    {/* "✓2 sent · next: step 3 …" -- without the sent count, a
                        follow-up's schedule reads like the FIRST email is
                        still waiting (a real support incident). */}
                    {enrollment.current_step > 0 ? (
                      <span className="mono small">✓{enrollment.current_step} sent</span>
                    ) : null}
                    <span className={`badge ${STATUS_TONE[enrollment.status] ?? ''}`}>
                      {enrollment.status}
                    </span>
                  </span>
                </div>
                <div className="enrol-line muted small">
                  <span className="enrol-next">{trailer(enrollment)}</span>
                  {canReenrol(enrollment) ? (
                    <button
                      type="button"
                      className="tiny"
                      disabled={reenrollingId === enrollment.id}
                      onClick={() => onReenrol(enrollment)}
                      data-testid="enrollment-reenrol"
                    >
                      Re-enrol
                    </button>
                  ) : null}
                </div>
              </li>
            ))}
            {!shown.length ? <li className="muted small" style={{ padding: 12 }}>Nobody matches.</li> : null}
          </ul>
          </div>
        </>
      ) : null}
    </div>
  );
}
