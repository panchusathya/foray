/**
 * Sequence email currently in flight: due, held, scheduled, or sending.
 *
 * Pattern follows DiscoveredTray: filter bar, useAsync list, Map selection,
 * a bulk bar that hides until something is selected, per-row buttons, and a
 * self-terminating poll while anything is actually moving.
 */

import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { IN_FLIGHT_STATE_LABELS, api } from '../lib/api.js';
import { countOnPage, toggleOne, togglePage } from '../lib/selection.js';
import { useAsync } from '../lib/useAsync.js';

const byId = (row) => row.enrollment_id;
const PAGE_SIZE = 100;

const STATE_TONE = {
  sending: 'strong',
  due: 'accent',
  held: 'miss',
  scheduled: 'partial',
};

const REASON_LABELS = {
  org_paused: 'org paused',
  sequence_paused: 'sequence paused',
  daily_cap: 'daily cap',
};

function formatDue(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function confirmCancel(action, count) {
  if (action === 'stop') {
    return window.confirm(
      `Stop enrolment for ${count} ${count === 1 ? 'person' : 'people'}? They will not get later steps either. This cannot be undone.`,
    );
  }
  return window.confirm(
    `Skip this email for ${count} ${count === 1 ? 'person' : 'people'}? The sequence continues at the next step. That skipped step cannot be sent later.`,
  );
}

export function OutboxPage() {
  const [state, setState] = useState('');
  const [sequenceId, setSequenceId] = useState('');
  const [selected, setSelected] = useState(new Map());
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const [error, setError] = useState(null);
  const [gaveUp, setGaveUp] = useState(false);

  const sequences = useAsync(() => api.listSequences(), []);
  const params = {
    state: state || undefined,
    sequence_id: sequenceId || undefined,
    limit: PAGE_SIZE,
  };
  const key = JSON.stringify(params);
  const list = useAsync(() => api.listInFlight(params), [key]);
  const data = list.data?.rows ?? [];
  const total = list.data?.total ?? 0;
  const pageSelected = countOnPage(selected, data, byId);

  useEffect(() => {
    setSelected(new Map());
  }, [key]);

  const startedAt = useRef(Date.now());
  const reload = list.reload;
  useEffect(() => {
    startedAt.current = Date.now();
    setGaveUp(false);
    let cancelled = false;
    let timer;

    async function tick() {
      if (cancelled) return;
      const rows = list.data?.rows ?? [];
      const busyNow = rows.some((row) => row.state === 'sending' || row.state === 'due');
      if (!busyNow) return;
      const elapsed = Date.now() - startedAt.current;
      if (elapsed > 10 * 60_000) {
        setGaveUp(true);
        return;
      }
      timer = setTimeout(async () => {
        if (cancelled) return;
        try {
          await reload();
        } catch {
          // A failed poll is not worth surfacing; the next one may work.
        }
        tick();
      }, elapsed > 2 * 60_000 ? 15_000 : 5_000);
    }

    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [key, list.data, reload]);

  useEffect(() => {
    const onFocus = () => reload();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [reload]);

  async function act(label, ids, action) {
    if (!ids.length) return;
    if (!confirmCancel(action, ids.length)) return;
    setBusy(label);
    setError(null);
    try {
      const result = await api.cancelInFlight({ enrollment_ids: ids, action });
      const moved = action === 'stop' ? result.stopped : result.skipped;
      const bits = [`${moved} ${action === 'stop' ? 'stopped' : 'skipped'}.`];
      if (result.already_sending) {
        bits.push(
          `${result.already_sending} already handing to the provider and could not be recalled.`,
        );
      }
      setNote(bits.join(' '));
      setSelected(new Map());
      await reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy('');
    }
  }

  const cancellable = [...selected.values()].filter((row) => row.state !== 'sending');

  return (
    <div className="stack" data-testid="outbox-page">
      <PageHeader
        title="Outbox"
        subtitle="Every sequence email in flight. Pause sending in Settings; skip or stop a row here."
      />

      <div className="row wrap" style={{ gap: 12 }}>
        <label className="row small" style={{ gap: 6 }}>
          State
          <select
            value={state}
            data-testid="outbox-state"
            onChange={(event) => setState(event.target.value)}
          >
            <option value="">All</option>
            {Object.entries(IN_FLIGHT_STATE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="row small" style={{ gap: 6 }}>
          Sequence
          <select
            value={sequenceId}
            data-testid="outbox-sequence"
            onChange={(event) => setSequenceId(event.target.value)}
          >
            <option value="">All sequences</option>
            {(sequences.data ?? []).map((sequence) => (
              <option key={sequence.id} value={sequence.id}>
                {sequence.name}
              </option>
            ))}
          </select>
        </label>
        <span className="muted small">
          {total ? `${total.toLocaleString()} in flight` : ''}
        </span>
        {gaveUp ? <span className="muted small">Stopped refreshing — reload to check.</span> : null}
      </div>

      {selected.size ? (
        <div className="card row spread wrap" data-testid="outbox-actions" style={{ gap: 10 }}>
          <span className="badge accent" data-testid="outbox-selected">
            {selected.size} selected
          </span>
          <span className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className="tiny ghost"
              disabled={!!busy || !cancellable.length}
              onClick={() => act('skip', cancellable.map(byId), 'skip')}
              data-testid="outbox-bulk-skip"
            >
              {busy === 'skip' ? 'Skipping…' : 'Skip this email'}
            </button>
            <button
              type="button"
              className="tiny ghost"
              disabled={!!busy || !cancellable.length}
              onClick={() => act('stop', cancellable.map(byId), 'stop')}
              data-testid="outbox-bulk-stop"
            >
              {busy === 'stop' ? 'Stopping…' : 'Stop enrolment'}
            </button>
            <button type="button" className="tiny ghost" onClick={() => setSelected(new Map())}>
              Clear
            </button>
          </span>
        </div>
      ) : null}

      {note ? (
        <div className="card small" data-testid="outbox-result">
          {note}
        </div>
      ) : null}

      <ErrorBox error={error} />
      <ErrorBox error={list.error} />

      {list.loading && !data.length ? (
        <Loading label="Loading outbox…" />
      ) : data.length ? (
        <div className="card dgrid-wrap">
          <table className="dgrid" data-testid="outbox-grid">
            <thead>
              <tr>
                <th className="pick">
                  <input
                    type="checkbox"
                    aria-label="Select page"
                    data-testid="outbox-select-page"
                    checked={data.length > 0 && pageSelected === data.length}
                    onChange={() => setSelected(togglePage(selected, data, byId))}
                  />
                </th>
                <th>Name</th>
                <th>Email</th>
                <th>Sequence</th>
                <th>Step</th>
                <th>Mailbox</th>
                <th>Due</th>
                <th>State</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.map((row) => (
                <tr
                  key={row.enrollment_id}
                  className={selected.has(row.enrollment_id) ? 'selected' : undefined}
                >
                  <td className="pick">
                    <input
                      type="checkbox"
                      aria-label={`Select ${row.candidate_name}`}
                      checked={selected.has(row.enrollment_id)}
                      onChange={() => setSelected(toggleOne(selected, row, byId))}
                    />
                  </td>
                  <td>
                    <Link to={`/contacts/${row.candidate_id}`}>{row.candidate_name}</Link>
                  </td>
                  <td>{row.candidate_email || '—'}</td>
                  <td>
                    <Link to={`/sequences/${row.sequence_id}`}>{row.sequence_name}</Link>
                  </td>
                  <td>{row.step + 1}</td>
                  <td>{row.mailbox || '—'}</td>
                  <td>{formatDue(row.due_at)}</td>
                  <td>
                    <Badge tone={STATE_TONE[row.state] || ''}>
                      {IN_FLIGHT_STATE_LABELS[row.state] || row.state}
                      {row.reason ? ` · ${REASON_LABELS[row.reason] || row.reason}` : ''}
                    </Badge>
                  </td>
                  <td>
                    {row.state === 'sending' ? (
                      <span className="muted small">sending</span>
                    ) : (
                      <span className="row" style={{ gap: 6 }}>
                        <button
                          type="button"
                          className="tiny ghost"
                          disabled={!!busy}
                          onClick={() => act('skip', [row.enrollment_id], 'skip')}
                        >
                          Skip
                        </button>
                        <button
                          type="button"
                          className="tiny ghost"
                          disabled={!!busy}
                          onClick={() => act('stop', [row.enrollment_id], 'stop')}
                        >
                          Stop
                        </button>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty>Nothing in flight.</Empty>
      )}
    </div>
  );
}
