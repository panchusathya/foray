import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../../components/common.jsx';
import { api } from '../../lib/api.js';
import { intakeApi } from '../../lib/intake.js';
import { useAsync } from '../../lib/useAsync.js';

function when(value) {
  return value ? new Date(value).toLocaleString() : '—';
}

function AttributeForm({ call, onDone }) {
  const [query, setQuery] = useState('');
  const [picked, setPicked] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const people = useAsync(
    () => (query.length >= 2 ? api.listCandidates({ q: query, limit: 8 }) : Promise.resolve([])),
    [query],
  );

  async function submit(event) {
    event.preventDefault();
    if (!picked) return;
    setSaving(true);
    setError(null);
    try {
      await intakeApi.patchCall(call.id, { candidate_id: picked });
      onDone();
    } catch (caught) {
      setError(caught);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="stack" style={{ gap: 6 }} onSubmit={submit}>
      <ErrorBox error={error} />
      <input
        type="search"
        placeholder="Search a name to attribute"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {(people.data ?? []).map((person) => (
        <label key={person.id} className="row" style={{ gap: 8 }}>
          <input
            type="radio"
            name={`attr-${call.id}`}
            checked={picked === person.id}
            onChange={() => setPicked(person.id)}
          />
          <span>
            {person.full_name}
            <span className="muted small"> {person.current_title}</span>
          </span>
        </label>
      ))}
      <button type="submit" className="tiny" disabled={!picked || saving}>
        Attribute
      </button>
    </form>
  );
}

export function IntakeReview() {
  const calls = useAsync(() => intakeApi.listCalls({ review_needed: true }), []);
  const rows = calls.data ?? [];

  return (
    <div className="stack" data-testid="intake-review">
      {calls.loading ? <Loading label="Loading review queue…" /> : null}
      <ErrorBox error={calls.error} />
      {!calls.loading && !rows.length ? (
        <Empty>Nothing waiting on review.</Empty>
      ) : (
        rows.map((row) => (
          <div key={row.id} className="card stack" data-testid="intake-review-row">
            <div className="row spread">
              <div>
                <Badge tone="partial">{row.status}</Badge>
                <span className="small muted"> {when(row.started_at || row.created_at)}</span>
                <div className="small">{row.review_reason || 'Needs a look.'}</div>
              </div>
              {row.candidate_id ? (
                <Link to={`/contacts/${row.candidate_id}`}>Open record</Link>
              ) : (
                <span className="muted small">unattributed</span>
              )}
            </div>
            {!row.candidate_id ? <AttributeForm call={row} onDone={() => calls.reload()} /> : null}
          </div>
        ))
      )}
    </div>
  );
}
