/**
 * People who may be in the CRM twice.
 *
 * Suggestions only, strongest evidence first. Merging cannot be undone once
 * notes, mail and enrolments have moved onto the survivor, so the survivor is
 * picked explicitly rather than defaulted to whichever row came back first,
 * and the confirm says what will actually happen.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';
import { Empty, ErrorBox, Loading } from './common.jsx';

const CONFIDENCE_LABELS = {
  same_company: 'same company',
  work_and_personal: 'work + personal',
  name_only: 'name only',
};

const CONFIDENCE_TONES = {
  same_company: 'strong',
  work_and_personal: 'accent',
  name_only: 'partial',
};

function Group({ group, onResolved }) {
  const [primary, setPrimary] = useState(group.candidates[0]?.id ?? '');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState(null);

  async function act(label, work, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(label);
    setError(null);
    try {
      await work();
      onResolved();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy('');
    }
  }

  const survivor = group.candidates.find((c) => c.id === primary);
  const losing = group.candidates.filter((c) => c.id !== primary);

  return (
    <div className="card stack" data-testid="duplicate-group">
      <div className="row spread wrap">
        <span className="row" style={{ gap: 8 }}>
          <strong>{group.name}</strong>
          <span className={`badge ${CONFIDENCE_TONES[group.confidence] ?? ''}`}>
            {CONFIDENCE_LABELS[group.confidence] ?? group.confidence}
          </span>
        </span>
        <span className="muted small">{group.reason}</span>
      </div>

      <table className="dgrid">
        <tbody>
          {group.candidates.map((candidate) => (
            <tr key={candidate.id} data-testid="duplicate-row">
              <td className="pick">
                <input
                  type="radio"
                  name={`primary-${group.candidates.map((c) => c.id).join('-')}`}
                  aria-label={`Keep ${candidate.full_name}`}
                  checked={primary === candidate.id}
                  onChange={() => setPrimary(candidate.id)}
                />
              </td>
              <td>
                <Link to={`/contacts/${candidate.id}`}>{candidate.full_name}</Link>
              </td>
              <td className="mono muted">{(candidate.emails ?? []).join(', ') || '—'}</td>
              <td>
                {[candidate.current_title, candidate.current_company]
                  .filter(Boolean)
                  .join(' · ') || <span className="muted">—</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <ErrorBox error={error} />

      <div className="row spread">
        <span className="muted small">
          Keeping <strong>{survivor?.full_name}</strong>; the other{' '}
          {losing.length === 1 ? 'record' : 'records'} will be folded in.
        </span>
        <span className="row" style={{ gap: 8 }}>
          <button
            type="button"
            className="ghost"
            disabled={!!busy}
            data-testid="duplicate-dismiss"
            onClick={() =>
              act('dismiss', () =>
                api.dismissDuplicate({
                  candidate_ids: group.candidates.map((c) => c.id),
                }),
              )
            }
          >
            Not the same person
          </button>
          <button
            type="button"
            disabled={!!busy || !primary || !losing.length}
            data-testid="duplicate-merge"
            onClick={() =>
              act(
                'merge',
                () =>
                  api.mergeDuplicates({
                    primary_id: primary,
                    duplicate_ids: losing.map((c) => c.id),
                  }),
                `Merge ${losing.length + 1} records into ${survivor?.full_name}? ` +
                  'Their email, notes, tags and pipeline history move across. ' +
                  'This cannot be undone.',
              )
            }
          >
            {busy === 'merge' ? 'Merging…' : 'Merge'}
          </button>
        </span>
      </div>
    </div>
  );
}

export function DuplicatesPanel() {
  const groups = useAsync(() => api.listDuplicates(), []);
  const [note, setNote] = useState('');

  const rows = groups.data ?? [];

  return (
    <div className="stack" data-testid="duplicates-panel">
      <ErrorBox error={groups.error} />
      {note ? (
        <div className="card small" data-testid="duplicate-result">
          {note}
        </div>
      ) : null}

      {groups.loading ? (
        <Loading label="Looking for duplicates…" />
      ) : rows.length ? (
        rows.map((group) => (
          <Group
            key={group.candidates.map((c) => c.id).join('-')}
            group={group}
            onResolved={() => {
              setNote('Done. Recalculating the rest…');
              groups.reload();
            }}
          />
        ))
      ) : (
        <Empty>
          Nobody looks duplicated. Two records are only suggested when they share a full
          name — a single address always belongs to one person already.
        </Empty>
      )}
    </div>
  );
}
