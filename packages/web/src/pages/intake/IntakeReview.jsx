import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../../components/common.jsx';
import { api } from '../../lib/api.js';
import { intakeApi } from '../../lib/intake.js';
import { useAsync } from '../../lib/useAsync.js';

function when(value) {
  return value ? new Date(value).toLocaleString() : '—';
}

function AttributeForm({ row, attribute, onDone }) {
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
      await attribute(row.id, { candidate_id: picked });
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
            name={`attr-${row.id}`}
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

function ThreadRow({ row, onDone }) {
  const who = row.contact_name || row.contact_email || row.from_address;
  return (
    <div className="card stack" data-testid="intake-review-thread">
      <div className="row spread">
        <div>
          <Badge tone="partial">{row.channel}</Badge>
          <span className="small muted"> {when(row.last_inbound_at || row.created_at)}</span>
          <div className="small">{row.review_reason || 'Needs a look.'}</div>
          <div className="small muted">
            {who || 'no contact details'} · {row.inbound_count} in / {row.outbound_count} out
          </div>
        </div>
        {row.candidate_id ? (
          <Link to={`/contacts/${row.candidate_id}`}>Open record</Link>
        ) : (
          <span className="muted small">unattributed</span>
        )}
      </div>
      {!row.candidate_id ? (
        <AttributeForm row={row} attribute={intakeApi.patchThread} onDone={onDone} />
      ) : null}
    </div>
  );
}

export function IntakeReview() {
  const calls = useAsync(() => intakeApi.listCalls({ review_needed: true }), []);
  // Threads sit on the same queue. IntakeThread.review_needed defaults to
  // true, so leaving them out meant every chat was flagged and none shown.
  const threads = useAsync(() => intakeApi.listThreads({ review_needed: true }), []);
  const rows = calls.data ?? [];
  const threadRows = threads.data ?? [];
  const loading = calls.loading || threads.loading;

  return (
    <div className="stack" data-testid="intake-review">
      {loading ? <Loading label="Loading review queue…" /> : null}
      <ErrorBox error={calls.error} />
      <ErrorBox error={threads.error} />
      {!loading && !rows.length && !threadRows.length ? (
        <Empty>Nothing waiting on review.</Empty>
      ) : (
        <>
          {threadRows.map((row) => (
            <ThreadRow key={row.id} row={row} onDone={() => threads.reload()} />
          ))}
          {rows.map((row) => (
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
              {!row.candidate_id ? (
                <AttributeForm
                  row={row}
                  attribute={intakeApi.patchCall}
                  onDone={() => calls.reload()}
                />
              ) : null}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
