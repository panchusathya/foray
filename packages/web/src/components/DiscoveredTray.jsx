/**
 * The review tray: people we have emailed who are not in Contacts yet.
 *
 * Sorted by recency of contact by default, and filtered to `new` — mixing in
 * the people who are already in Contacts would bury the strangers the sweep
 * exists to surface.
 *
 * Progress is polled rather than pushed. There is no realtime plumbing here,
 * and no percentage either: the total size of a mailbox is not known up front,
 * so a percentage would be invented. A rising "scanned" count is honest.
 */

import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../lib/api.js';
import { countOnPage, selectAllFrom, togglePage, toggleOne } from '../lib/selection.js';
import { useAsync } from '../lib/useAsync.js';
import { Empty, ErrorBox, Loading } from './common.jsx';

const byId = (row) => row.id;
const PAGE_SIZE = 100;

const FLAG_LABELS = {
  revive: 'gone quiet',
  one_touch: 'one touch',
  cc_only: 'cc only',
  role_account: 'role account',
  suppressed: 'do not contact',
};

function shortDate(value) {
  if (!value) return '—';
  return new Date(value).toLocaleDateString();
}

/** 5s while a sweep runs, backing off, and stopping rather than polling forever. */
function useSweepStatus() {
  const [sweeps, setSweeps] = useState([]);
  const [gaveUp, setGaveUp] = useState(false);
  const startedAt = useRef(Date.now());

  useEffect(() => {
    let cancelled = false;
    let timer;

    async function tick() {
      if (cancelled) return;
      let running = false;
      try {
        const rows = (await api.mailboxActivity()).map((row) => ({ ...row, ...row.discovery }));
        if (cancelled) return;
        setSweeps(rows);
        running = rows.some((row) => ['queued', 'running'].includes(row.status));
      } catch {
        // A failed poll is not worth surfacing; the next one may work.
      }
      if (cancelled) return;
      const elapsed = Date.now() - startedAt.current;
      if (!running) return;
      if (elapsed > 10 * 60_000) {
        setGaveUp(true);
        return;
      }
      timer = setTimeout(tick, elapsed > 2 * 60_000 ? 15_000 : 5_000);
    }

    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);

  return { sweeps, gaveUp };
}

export function DiscoveredTray() {
  const [minMessages, setMinMessages] = useState(1);
  const [includeCcOnly, setIncludeCcOnly] = useState(false);
  const [status, setStatus] = useState('new');
  const [selected, setSelected] = useState(new Map());
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const [error, setError] = useState(null);

  const params = { status, min_messages: minMessages, include_cc_only: includeCcOnly };
  const key = JSON.stringify(params);
  const rows = useAsync(() => api.listDiscovered({ ...params, limit: PAGE_SIZE }), [key]);
  const total = useAsync(() => api.discoveredCount(params), [key]);
  const { sweeps, gaveUp } = useSweepStatus();

  const data = rows.data ?? [];
  const pageSelected = countOnPage(selected, data, byId);

  useEffect(() => {
    setSelected(new Map());
  }, [key]);

  async function act(label, work) {
    setBusy(label);
    setError(null);
    try {
      setNote(await work());
      setSelected(new Map());
      rows.reload();
      total.reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy('');
    }
  }

  function promote() {
    const ids = [...selected.keys()];
    act('promote', async () => {
      const result = await api.promoteDiscovered({ ids });
      const failed = Object.keys(result.failed ?? {}).length;
      return (
        `Promoted ${result.promoted} into Contacts. Their email history is importing.` +
        (failed ? ` ${failed} could not be created.` : '')
      );
    });
  }

  function dismiss() {
    const ids = [...selected.keys()];
    if (!window.confirm(`Dismiss ${ids.length}? They stay out of future sweeps.`)) return;
    act('dismiss', async () => {
      const result = await api.dismissDiscovered({ ids });
      return `Dismissed ${result.dismissed}.`;
    });
  }

  function ignoreDomain(domain) {
    if (!window.confirm(`Ignore everyone at ${domain}, now and in future sweeps?`)) return;
    act('domain', async () => {
      const result = await api.ignoreDomain({ domain });
      return `Ignored ${domain} and dismissed ${result.dismissed}.`;
    });
  }

  const active = sweeps.filter((row) => ['queued', 'running'].includes(row.status));

  return (
    <div className="stack" data-testid="discovered-tray">
      {active.length ? (
        <div className="card small tray-banner" data-testid="sweep-status">
          {active.map((row) => (
            <div key={row.mailbox_id}>
              Sweeping {row.address} — {row.scanned.toLocaleString()} messages scanned,{' '}
              {row.found.toLocaleString()} people found
            </div>
          ))}
          {gaveUp ? <div className="muted">Still running — refresh to check.</div> : null}
        </div>
      ) : null}

      <div className="row wrap" style={{ gap: 12 }}>
        <label className="row small" style={{ gap: 6 }}>
          Show
          <select
            value={status}
            data-testid="tray-status"
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="new">New</option>
            <option value="already_known">Already in Contacts</option>
            <option value="dismissed">Dismissed</option>
          </select>
        </label>
        <label className="row small" style={{ gap: 6 }}>
          At least
          <select
            value={minMessages}
            data-testid="tray-min-messages"
            onChange={(event) => setMinMessages(Number(event.target.value))}
          >
            {[1, 2, 3, 5, 10].map((n) => (
              <option key={n} value={n}>
                {n} message{n === 1 ? '' : 's'}
              </option>
            ))}
          </select>
        </label>
        <label className="row small" style={{ gap: 6 }}>
          <input
            type="checkbox"
            checked={includeCcOnly}
            data-testid="tray-cc-only"
            onChange={(event) => setIncludeCcOnly(event.target.checked)}
          />
          Include cc-only
        </label>
        <span className="muted small">
          {total.data?.count != null ? `${total.data.count.toLocaleString()} to review` : ''}
        </span>
      </div>

      {selected.size ? (
        <div className="card row spread wrap" data-testid="tray-actions" style={{ gap: 10 }}>
          <span className="badge accent" data-testid="tray-selected">
            {selected.size} selected
          </span>
          <span className="row" style={{ gap: 8 }}>
            <button type="button" disabled={!!busy} onClick={promote} data-testid="tray-promote">
              {busy === 'promote' ? 'Promoting…' : 'Promote'}
            </button>
            <button
              type="button"
              className="ghost"
              disabled={!!busy}
              onClick={dismiss}
              data-testid="tray-dismiss"
            >
              Dismiss
            </button>
            <button
              type="button"
              className="tiny ghost"
              onClick={() => setSelected(new Map())}
            >
              Clear
            </button>
          </span>
        </div>
      ) : null}

      {note ? (
        <div className="card small" data-testid="tray-result">
          {note} <Link to="/contacts?view=recent">View them →</Link>
        </div>
      ) : null}

      <ErrorBox error={error} />
      <ErrorBox error={rows.error} />

      {rows.loading ? (
        <Loading label="Loading discovered contacts…" />
      ) : data.length ? (
        <div className="card dgrid-wrap">
          <table className="dgrid" data-testid="discovered-grid">
            <thead>
              <tr>
                <th className="pick">
                  <input
                    type="checkbox"
                    aria-label="Select page"
                    data-testid="tray-select-page"
                    checked={data.length > 0 && pageSelected === data.length}
                    onChange={() => setSelected(togglePage(selected, data, byId))}
                  />
                </th>
                <th>Name</th>
                <th>Email</th>
                <th>Messages</th>
                <th>Threads</th>
                <th>Last contacted</th>
                <th>Flags</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.map((row) => (
                <tr
                  key={row.id}
                  data-testid="discovered-row"
                  className={selected.has(row.id) ? 'selected' : undefined}
                >
                  <td className="pick">
                    <input
                      type="checkbox"
                      aria-label={`Select ${row.email}`}
                      checked={selected.has(row.id)}
                      onChange={() => setSelected(toggleOne(selected, row, byId))}
                    />
                  </td>
                  <td>{row.display_name || <span className="muted">—</span>}</td>
                  <td className="mono muted">{row.email}</td>
                  <td className="num tray-evidence">
                    {row.to_count}
                    <span className="muted">/</span>
                    {row.cc_count}
                  </td>
                  <td className="num tray-evidence">{row.thread_count}</td>
                  <td>{shortDate(row.last_seen_at)}</td>
                  <td>
                    {row.suggested_status ? (
                      <span className="badge">{FLAG_LABELS[row.suggested_status]}</span>
                    ) : null}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="tiny ghost"
                      title={`Ignore everyone at ${row.domain}`}
                      onClick={() => ignoreDomain(row.domain)}
                    >
                      Ignore {row.domain}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty>
          Nothing to review. Sweep a mailbox from <Link to="/settings">Settings</Link> to find
          people you have emailed who are not in Contacts yet.
        </Empty>
      )}

      {data.length && data.length < (total.data?.count ?? 0) ? (
        <button
          type="button"
          className="tiny ghost"
          onClick={() => setSelected(selectAllFrom(data, byId))}
        >
          Select all {data.length} on this page
        </button>
      ) : null}
    </div>
  );
}
