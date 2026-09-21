/**
 * "And when should it go instead?" -- asked once, for one row or a selection.
 *
 * Shared by the Outbox's Cancelled view and the sequence editor's enrolment
 * list, because a stop is undone wherever the recruiter happens to be looking
 * at the person: in the queue of what is about to send, or on the sequence
 * they were stopped in. Inline rather than a modal: the rows it is about stay
 * in view, and the default answer (the step's usual hour) needs no input at
 * all, so the common case is one click.
 *
 * Someone who replied to us meanwhile comes back in `skipped` rather than
 * being re-enrolled; they are named here with an "Enrol anyway" that
 * re-submits just them with `force`, the same bargain the enrol picker offers.
 *
 * `rows` carry `enrollment_id`, `candidate_id` and `candidate_name` -- the
 * Outbox's in-flight shape. The sequence editor adapts its EnrollmentOut rows
 * to that shape rather than the other way round, so this stays one component.
 */

import { useState } from 'react';

import { api } from '../lib/api.js';
import { REENROL_WHEN, defaultPickTime, describeReenrol, reenrolPayload } from '../lib/outbox.js';
import { ErrorBox } from './common.jsx';

const byId = (row) => row.enrollment_id;

export function ReenrolPanel({ rows, onClose, onDone }) {
  const [when, setWhen] = useState(REENROL_WHEN[0].id);
  const [at, setAt] = useState(() => defaultPickTime());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [blocked, setBlocked] = useState([]);

  async function submit({ force = false, onlyRows = null } = {}) {
    const targets = onlyRows ?? rows;
    let payload;
    try {
      payload = reenrolPayload({ ids: targets.map(byId), when, at, force });
    } catch (caught) {
      setError(caught);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await api.reenrollInFlight(payload);
      const note = describeReenrol(result, when);
      const skippedCandidates = new Set((result.skipped ?? []).map((row) => row.candidate_id));
      const stillStopped = targets.filter((row) => skippedCandidates.has(row.candidate_id));
      // Reported upward so the note outlives this panel, which closes once
      // nobody is left to ask about.
      await onDone(note);
      if (stillStopped.length) {
        setBlocked(stillStopped);
      } else {
        onClose();
      }
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  const who = rows.length === 1 && rows[0].candidate_name ? rows[0].candidate_name : rows.length;

  return (
    <div className="card stack" data-testid="reenrol-panel" style={{ gap: 10 }}>
      <div className="spread">
        <strong className="small">Re-enrol {who} — when should the email go?</strong>
        <button type="button" className="tiny ghost" onClick={onClose}>
          Close
        </button>
      </div>
      <div className="stack small" style={{ gap: 6 }}>
        {REENROL_WHEN.map((option) => (
          <label key={option.id} className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
            <input
              type="radio"
              name="reenrol-when"
              value={option.id}
              checked={when === option.id}
              onChange={() => setWhen(option.id)}
              data-testid={`reenrol-when-${option.id}`}
            />
            <span>
              {option.label}
              {option.hint ? <span className="muted"> — {option.hint}</span> : null}
              {option.id === 'at' && when === 'at' ? (
                <input
                  type="datetime-local"
                  value={at}
                  onChange={(event) => setAt(event.target.value)}
                  data-testid="reenrol-at"
                  style={{ display: 'block', marginTop: 4, maxWidth: '100%' }}
                />
              ) : null}
            </span>
          </label>
        ))}
      </div>
      <ErrorBox error={error} />
      {blocked.length ? (
        <div className="card small spread" style={{ margin: 0 }} data-testid="reenrol-blocked">
          <span>
            {blocked.map((row) => row.candidate_name).join(', ')} replied to you recently and{' '}
            {blocked.length === 1 ? 'was' : 'were'} left stopped.
          </span>
          <button
            type="button"
            className="tiny"
            disabled={busy}
            onClick={() => submit({ force: true, onlyRows: blocked })}
            data-testid="reenrol-force"
          >
            {busy ? 'Re-enrolling…' : 'Enrol anyway'}
          </button>
        </div>
      ) : (
        <div className="row" style={{ gap: 8 }}>
          <button
            type="button"
            className="tiny primary"
            disabled={busy}
            onClick={() => submit()}
            data-testid="reenrol-submit"
          >
            {busy ? 'Re-enrolling…' : `Re-enrol ${rows.length === 1 ? '' : rows.length}`.trim()}
          </button>
        </div>
      )}
    </div>
  );
}
