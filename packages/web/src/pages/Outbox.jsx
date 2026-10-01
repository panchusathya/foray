/**
 * Sequence email currently in flight: due, held, scheduled, or sending -- and
 * the Cancelled view, where a stop can be undone.
 *
 * Pattern follows DiscoveredTray: filter bar, useAsync list, Map selection,
 * a bulk bar that hides until something is selected, per-row buttons, and a
 * self-terminating poll while anything is actually moving.
 *
 * Cancelled rows share the grid but not the default view. "Stop" turned out
 * to mean "not at that time" as often as "not at all", so a stopped row is
 * kept one click away with a re-enrol that asks the only question that
 * matters: when should it go instead?
 */

import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { ColGroup, ResizeHandle, useColumnWidths } from '../components/ResizableColumns.jsx';
import { MultiSelect } from '../components/MultiSelect.jsx';
import { ReenrolPanel } from '../components/ReenrolPanel.jsx';
import { Badge, Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { IN_FLIGHT_STATE_LABELS, api } from '../lib/api.js';
import { countOnPage, toggleOne, togglePage, toggleRange } from '../lib/selection.js';
import { useAsync } from '../lib/useAsync.js';

const byId = (row) => row.enrollment_id;
const PAGE_SIZE = 100;

// Starting widths in px; each column can be dragged wider or narrower.
const COLUMN_WIDTHS = {
  pick: 40,
  rank: 56,
  name: 180,
  email: 230,
  sequence: 230,
  step: 64,
  mailbox: 200,
  due: 170,
  state: 230,
  actions: 330,
};
const COLUMNS = Object.keys(COLUMN_WIDTHS);

// What goes first when the line is backed up (org setting, editable here).
const PRIORITY_LABELS = {
  first_emails: 'First emails before follow-ups',
  follow_ups: 'Follow-ups before first emails',
  queue: 'Queue order (no step priority)',
};

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
  mailbox_full: 'waiting for mailbox room',
  outside_hours: 'outside sending hours',
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
      `Stop enrolment for ${count} ${count === 1 ? 'person' : 'people'}? They will not get later steps either. You can re-enrol them later from the Cancelled view.`,
    );
  }
  return window.confirm(
    `Skip this email for ${count} ${count === 1 ? 'person' : 'people'}? The sequence continues at the next step. That skipped step cannot be sent later.`,
  );
}

export function OutboxPage() {
  const [state, setState] = useState('');
  // Empty means every sequence; otherwise only these.
  const [sequenceIds, setSequenceIds] = useState([]);
  const [selected, setSelected] = useState(new Map());
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const [error, setError] = useState(null);
  const [gaveUp, setGaveUp] = useState(false);
  // The rows a ReenrolPanel is open for; null when it is closed.
  const [reenrolling, setReenrolling] = useState(null);
  const [page, setPage] = useState(0);
  const [position, setPosition] = useState('');
  // Anchor for shift-click: the row index last clicked on this page.
  const lastIndex = useRef(null);

  const sequences = useAsync(() => api.listSequences(), []);
  const sending = useAsync(() => api.orgSending(), []);
  const columns = useColumnWidths('outbox.columns', COLUMN_WIDTHS);
  const params = {
    state: state || undefined,
    sequence_id: sequenceIds.length ? sequenceIds : undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  };
  const key = JSON.stringify(params);
  const list = useAsync(() => api.listInFlight(params), [key]);
  const data = list.data?.rows ?? [];
  const total = list.data?.total ?? 0;
  const cancelledTotal = list.data?.cancelled_total ?? 0;
  const viewingCancelled = state === 'cancelled';
  const pageSelected = countOnPage(selected, data, byId);

  useEffect(() => {
    setSelected(new Map());
    setReenrolling(null);
    lastIndex.current = null;
  }, [key]);

  // A filter change starts again at the first page.
  useEffect(() => setPage(0), [state, sequenceIds]);
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  function onPick(row, index, event) {
    if (event.shiftKey && lastIndex.current != null) {
      setSelected(toggleRange(selected, data, lastIndex.current, index, byId));
    } else {
      setSelected(toggleOne(selected, row, byId));
    }
    lastIndex.current = index;
  }

  async function changePriority(value) {
    setBusy('priority');
    setError(null);
    try {
      await api.patchOrgSending({ send_priority: value });
      setNote(`Line re-sorted: ${PRIORITY_LABELS[value].toLowerCase()}.`);
      await Promise.all([sending.reload(), reload()]);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy('');
    }
  }

  async function move(to, ids, at) {
    if (!ids.length) return;
    setBusy(`move-${to}`);
    setError(null);
    try {
      const body = { enrollment_ids: ids, to };
      if (to === 'position') body.position = at;
      const result = await api.moveInFlight(body);
      setNote(
        result.moved
          ? `Moved ${result.moved} ${result.moved === 1 ? 'email' : 'emails'}.`
          : 'Nothing to move — only queued emails have a place in line.',
      );
      await reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy('');
    }
  }

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

  const cancellable = [...selected.values()].filter(
    (row) => row.state !== 'sending' && row.state !== 'cancelled',
  );
  const resumable = [...selected.values()].filter((row) => row.state === 'cancelled');
  // Only queued rows have a rank; the rest go at their scheduled time.
  const movable = [...selected.values()].filter((row) => row.rank != null).map(byId);

  return (
    <div className="stack" data-testid="outbox-page">
      <PageHeader
        title="Outbox"
        subtitle="Every sequence email in flight, in the order it will go (#1 is next). Select rows to move them up or down the line; shift-click selects a range. Pause sending in Settings; skip or stop a row here, and re-enrol a stopped one from the Cancelled view."
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
        <div className="row small" style={{ gap: 6 }}>
          Sequences
          <MultiSelect
            testId="outbox-sequence"
            allLabel="All sequences"
            noun="sequences"
            value={sequenceIds}
            onChange={setSequenceIds}
            options={(sequences.data ?? []).map((sequence) => ({
              value: sequence.id,
              label: sequence.name,
            }))}
          />
        </div>
        <label
          className="row small"
          style={{ gap: 6 }}
          title="When the line is backed up, which emails go first. New arrivals join behind their own step and ahead of the steps after it; rows you move by hand stay where you put them."
        >
          Priority
          <select
            value={sending.data?.send_priority ?? 'first_emails'}
            data-testid="outbox-priority"
            disabled={!sending.data || busy === 'priority'}
            onChange={(event) => changePriority(event.target.value)}
          >
            {Object.entries(PRIORITY_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <span className="muted small">
          {total ? `${total.toLocaleString()} ${viewingCancelled ? 'cancelled' : 'in flight'}` : ''}
        </span>
        {/* The live view says out loud that stopped rows exist and can come
            back; otherwise "Stop" reads as final and nobody looks for undo. */}
        {!viewingCancelled && cancelledTotal ? (
          <button
            type="button"
            className="tiny ghost"
            onClick={() => setState('cancelled')}
            data-testid="outbox-cancelled-hint"
          >
            {cancelledTotal.toLocaleString()} cancelled — re-enrol
          </button>
        ) : null}
        {gaveUp ? <span className="muted small">Stopped refreshing — reload to check.</span> : null}
      </div>

      {selected.size ? (
        <div className="card row spread wrap" data-testid="outbox-actions" style={{ gap: 10 }}>
          <span className="badge accent" data-testid="outbox-selected">
            {selected.size} selected
          </span>
          <span className="row" style={{ gap: 8 }}>
            {resumable.length ? (
              <button
                type="button"
                className="tiny primary"
                disabled={!!busy}
                onClick={() => setReenrolling(resumable)}
                data-testid="outbox-bulk-reenrol"
              >
                Re-enrol {resumable.length}
              </button>
            ) : null}
            {movable.length ? (
              <span className="row" style={{ gap: 6 }} data-testid="outbox-move">
                <span className="muted small">Move {movable.length}:</span>
                <button type="button" className="tiny ghost" disabled={!!busy} onClick={() => move('top', movable)}>
                  To top
                </button>
                <button type="button" className="tiny ghost" disabled={!!busy} onClick={() => move('up', movable)}>
                  Up
                </button>
                <button type="button" className="tiny ghost" disabled={!!busy} onClick={() => move('down', movable)}>
                  Down
                </button>
                <button type="button" className="tiny ghost" disabled={!!busy} onClick={() => move('bottom', movable)}>
                  To bottom
                </button>
                <form
                  className="row"
                  style={{ gap: 4 }}
                  onSubmit={(event) => {
                    event.preventDefault();
                    const at = Number.parseInt(position, 10);
                    if (at >= 1) move('position', movable, at);
                  }}
                >
                  <input
                    type="number"
                    min="1"
                    placeholder="#"
                    aria-label="Move to position"
                    value={position}
                    onChange={(event) => setPosition(event.target.value)}
                    style={{ width: 70 }}
                  />
                  <button type="submit" className="tiny ghost" disabled={!!busy || !position}>
                    Move to #
                  </button>
                </form>
              </span>
            ) : null}
            {cancellable.length ? (
              <>
                <button
                  type="button"
                  className="tiny ghost"
                  disabled={!!busy}
                  onClick={() => act('skip', cancellable.map(byId), 'skip')}
                  data-testid="outbox-bulk-skip"
                >
                  {busy === 'skip' ? 'Skipping…' : 'Skip this email'}
                </button>
                <button
                  type="button"
                  className="tiny ghost"
                  disabled={!!busy}
                  onClick={() => act('stop', cancellable.map(byId), 'stop')}
                  data-testid="outbox-bulk-stop"
                >
                  {busy === 'stop' ? 'Stopping…' : 'Stop enrolment'}
                </button>
              </>
            ) : null}
            <button type="button" className="tiny ghost" onClick={() => setSelected(new Map())}>
              Clear
            </button>
          </span>
        </div>
      ) : null}

      {reenrolling ? (
        <ReenrolPanel
          rows={reenrolling}
          onClose={() => setReenrolling(null)}
          onDone={async (text) => {
            setNote(text);
            setSelected(new Map());
            await reload();
          }}
        />
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
          {/* min-width makes the grid wider than a narrow window instead of
              squeezing every column to an ellipsis, so the wrap scrolls
              sideways. Shift-click a checkbox to select a range. */}
          <table
            className="dgrid outbox-grid"
            data-testid="outbox-grid"
            style={{ width: columns.total }}
          >
            <ColGroup columns={COLUMNS} widths={columns.widths} />
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
                <th className="num" title="Place in line — 1 goes next">
                  #
                  <ResizeHandle column="rank" startResize={columns.startResize} reset={columns.reset} />
                </th>
                <th>
                  Name
                  <ResizeHandle column="name" startResize={columns.startResize} reset={columns.reset} />
                </th>
                <th>
                  Email
                  <ResizeHandle column="email" startResize={columns.startResize} reset={columns.reset} />
                </th>
                <th>
                  Sequence
                  <ResizeHandle column="sequence" startResize={columns.startResize} reset={columns.reset} />
                </th>
                <th>
                  Step
                  <ResizeHandle column="step" startResize={columns.startResize} reset={columns.reset} />
                </th>
                <th>
                  Mailbox
                  <ResizeHandle column="mailbox" startResize={columns.startResize} reset={columns.reset} />
                </th>
                <th>
                  {viewingCancelled ? 'Stopped' : 'Goes out'}
                  <ResizeHandle column="due" startResize={columns.startResize} reset={columns.reset} />
                </th>
                <th>
                  State
                  <ResizeHandle column="state" startResize={columns.startResize} reset={columns.reset} />
                </th>
                <th>
                  <ResizeHandle column="actions" startResize={columns.startResize} reset={columns.reset} />
                </th>
              </tr>
            </thead>
            <tbody>
              {data.map((row, index) => (
                <tr
                  key={row.enrollment_id}
                  className={selected.has(row.enrollment_id) ? 'selected' : undefined}
                >
                  <td className="pick">
                    <input
                      type="checkbox"
                      aria-label={`Select ${row.candidate_name}`}
                      checked={selected.has(row.enrollment_id)}
                      // Handled on click so shift-click reaches us; onChange
                      // stays a noop to keep React's controlled input happy.
                      onClick={(event) => onPick(row, index, event)}
                      onChange={() => {}}
                    />
                  </td>
                  <td className="num" data-testid="outbox-rank">
                    {row.rank ?? '—'}
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
                  <td>
                    {row.state === 'cancelled'
                      ? formatDue(row.cancelled_at)
                      : `${row.state === 'due' && row.rank != null ? '≈ ' : ''}${formatDue(row.due_at)}`}
                  </td>
                  <td>
                    <Badge tone={STATE_TONE[row.state] || ''}>
                      {IN_FLIGHT_STATE_LABELS[row.state] || row.state}
                      {row.reason ? ` · ${REASON_LABELS[row.reason] || row.reason}` : ''}
                    </Badge>
                  </td>
                  <td>
                    {row.state === 'sending' ? (
                      <span className="muted small">sending</span>
                    ) : row.state === 'cancelled' ? (
                      <button
                        type="button"
                        className="tiny"
                        disabled={!!busy}
                        onClick={() => setReenrolling([row])}
                        data-testid="outbox-row-reenrol"
                      >
                        Re-enrol
                      </button>
                    ) : (
                      <span className="row" style={{ gap: 6 }}>
                        {row.rank != null ? (
                          <>
                            <button
                              type="button"
                              className="tiny ghost"
                              title="Move to the top"
                              aria-label={`Move ${row.candidate_name} to the top`}
                              disabled={!!busy || row.rank === 1}
                              onClick={() => move('top', [row.enrollment_id])}
                            >
                              ⤒
                            </button>
                            <button
                              type="button"
                              className="tiny ghost"
                              title="Move up one"
                              aria-label={`Move ${row.candidate_name} up`}
                              disabled={!!busy || row.rank === 1}
                              onClick={() => move('up', [row.enrollment_id])}
                            >
                              ▲
                            </button>
                            <button
                              type="button"
                              className="tiny ghost"
                              title="Move down one"
                              aria-label={`Move ${row.candidate_name} down`}
                              disabled={!!busy}
                              onClick={() => move('down', [row.enrollment_id])}
                            >
                              ▼
                            </button>
                            <button
                              type="button"
                              className="tiny ghost"
                              title="Move to the bottom"
                              aria-label={`Move ${row.candidate_name} to the bottom`}
                              disabled={!!busy}
                              onClick={() => move('bottom', [row.enrollment_id])}
                            >
                              ⤓
                            </button>
                          </>
                        ) : null}
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
          {pageCount > 1 ? (
            <div className="row spread small" style={{ padding: '8px 12px' }} data-testid="outbox-pager">
              <button type="button" className="tiny ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>
                ← Previous
              </button>
              <span className="muted">
                Page {page + 1} of {pageCount}
              </span>
              <button
                type="button"
                className="tiny ghost"
                disabled={page + 1 >= pageCount}
                onClick={() => setPage(page + 1)}
              >
                Next →
              </button>
            </div>
          ) : null}
        </div>
      ) : viewingCancelled ? (
        <Empty>Nothing cancelled — rows you stop from the outbox land here, ready to re-enrol.</Empty>
      ) : (
        <Empty>Nothing in flight.</Empty>
      )}
    </div>
  );
}
