/**
 * The shared-candidates grid: one component for the client's own dashboard
 * and for our view of it.
 *
 * One component on purpose. The client and we are looking at the same rows
 * and talking about them in the same thread; two grids would drift until the
 * client and the recruiter on a call are describing different screens. What
 * differs by side is passed in -- `api` (which routes to call) and the
 * recruiter-only extras (selection and bulk moves, unshare, the CRM link).
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import {
  PILES,
  STAGES,
  downloadCsv,
  inPile,
  relativeTime,
  stageLabel,
  stageTone,
} from '../lib/clientStages.js';
import { ErrorBox } from './common.jsx';

function StageSelect({ value, onChange, disabled, testId }) {
  return (
    <select
      className={`cp-stage tone-${stageTone(value) || 'plain'}`}
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      onClick={(event) => event.stopPropagation()}
      data-testid={testId}
      aria-label="Stage"
    >
      {STAGES.map((s) => (
        <option key={s.id} value={s.id}>
          {s.label}
        </option>
      ))}
    </select>
  );
}

function AuthorTag({ kind, clientName }) {
  return (
    <span className={`badge ${kind === 'client' ? 'accent' : ''}`}>
      {kind === 'client' ? clientName : 'Foray'}
    </span>
  );
}

function LinkedInLink({ url }) {
  if (!url) return null;
  return (
    <a
      className="cp-li"
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      onClick={(event) => event.stopPropagation()}
      title="Open LinkedIn profile"
    >
      in
    </a>
  );
}

/** Title at company, whichever halves exist. */
function roleLine(row) {
  if (row.current_title && row.current_company)
    return `${row.current_title} · ${row.current_company}`;
  return row.current_title || row.current_company || '';
}

function Drawer({ rowId, side, api, clientName, onClose, onChanged, extras }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const composer = useRef(null);

  useEffect(() => {
    let live = true;
    setDetail(null);
    setError(null);
    api
      .loadDetail(rowId)
      .then((d) => live && setDetail(d))
      .catch((e) => live && setError(e));
    return () => {
      live = false;
    };
  }, [rowId, api]);

  useEffect(() => {
    const onKey = (event) => event.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  async function changeStage(stage) {
    setError(null);
    try {
      const updated = await api.setStage(rowId, stage);
      setDetail(updated);
      onChanged(updated);
    } catch (caught) {
      setError(caught);
    }
  }

  async function addNote(event) {
    event?.preventDefault();
    const body = draft.trim();
    if (!body) return;
    setBusy(true);
    setError(null);
    try {
      await api.addNote(rowId, body);
      setDraft('');
      const refreshed = await api.loadDetail(rowId);
      setDetail(refreshed);
      onChanged(refreshed);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  async function removeNote(noteId) {
    if (!window.confirm('Delete this note? Both sides will stop seeing it.')) return;
    setError(null);
    try {
      await api.deleteNote(noteId);
      const refreshed = await api.loadDetail(rowId);
      setDetail(refreshed);
      onChanged(refreshed);
    } catch (caught) {
      setError(caught);
    }
  }

  return (
    <div className="cp-drawer-overlay" onClick={onClose}>
      <aside
        className="cp-drawer"
        onClick={(event) => event.stopPropagation()}
        data-testid="cp-drawer"
        aria-label="Candidate details"
      >
        <div className="spread">
          <h2 className="cp-drawer-name">{detail?.full_name ?? '…'}</h2>
          <button type="button" className="tiny ghost" onClick={onClose} aria-label="Close">
            Close
          </button>
        </div>
        <ErrorBox error={error} />
        {detail ? (
          <>
            <div className="muted small">{roleLine(detail)}</div>
            {detail.location ? <div className="muted small">{detail.location}</div> : null}
            <div className="row cp-drawer-meta">
              {detail.linkedin_url ? (
                <a href={detail.linkedin_url} target="_blank" rel="noreferrer noopener">
                  LinkedIn profile ↗
                </a>
              ) : null}
              {detail.role_title ? <span className="badge">For: {detail.role_title}</span> : null}
              {side === 'recruiter' && detail.candidate_id ? (
                <Link to={`/contacts/${detail.candidate_id}`}>Contact record</Link>
              ) : null}
            </div>

            <label className="cp-field">
              <span className="sidebar-heading">Stage</span>
              <StageSelect
                value={detail.stage}
                onChange={changeStage}
                testId="cp-drawer-stage"
              />
            </label>

            <section className="stack cp-notes">
              <div className="sidebar-heading">
                Notes <span className="muted">· visible to {clientName} and Foray</span>
              </div>
              {detail.notes.length === 0 ? (
                <div className="muted small">No notes yet.</div>
              ) : (
                detail.notes.map((note) => (
                  <div key={note.id} className="cp-note" data-testid="cp-note">
                    <div className="spread small">
                      <span>
                        <strong>{note.author_name}</strong>{' '}
                        <AuthorTag kind={note.author_kind} clientName={clientName} />
                      </span>
                      <span className="muted" title={new Date(note.created_at).toLocaleString()}>
                        {relativeTime(note.created_at)}
                        {note.mine || side === 'recruiter' ? (
                          <button
                            type="button"
                            className="tiny ghost cp-note-delete"
                            onClick={() => removeNote(note.id)}
                          >
                            Delete
                          </button>
                        ) : null}
                      </span>
                    </div>
                    <div className="cp-note-body">{note.body}</div>
                  </div>
                ))
              )}
              <form className="stack" onSubmit={addNote}>
                <textarea
                  ref={composer}
                  rows={3}
                  placeholder="Add a note — feedback, next steps, scheduling…"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) addNote();
                  }}
                  data-testid="cp-note-input"
                />
                <div className="spread">
                  <span className="muted small">Ctrl/⌘ + Enter to save</span>
                  <button type="submit" className="primary" disabled={busy || !draft.trim()}>
                    {busy ? 'Saving…' : 'Add note'}
                  </button>
                </div>
              </form>
            </section>

            <section className="stack cp-history">
              <div className="sidebar-heading">History</div>
              {[...detail.events].reverse().map((event, index) => (
                <div key={index} className="small">
                  <span className="muted">{relativeTime(event.created_at)}</span>{' '}
                  {event.from_stage ? (
                    <>
                      {event.actor_name} moved {stageLabel(event.from_stage)} →{' '}
                      <strong>{stageLabel(event.to_stage)}</strong>
                    </>
                  ) : (
                    <>
                      Shared by {event.actor_name} as <strong>{stageLabel(event.to_stage)}</strong>
                    </>
                  )}
                </div>
              ))}
              <div className="muted small">
                Shared {new Date(detail.shared_at).toLocaleDateString()}
              </div>
            </section>

            {extras ? extras(detail) : null}
          </>
        ) : null}
      </aside>
    </div>
  );
}

/**
 * @param {object} props
 * @param {'client'|'recruiter'} props.side
 * @param {Array} props.rows
 * @param {object} props.api  {setStage, loadDetail, addNote, deleteNote, bulkStage?}
 * @param {string} props.clientName
 * @param {(rows) => void} props.onRowsChange  replace rows after a local edit
 * @param {(detail) => ReactNode} [props.drawerExtras]  recruiter-only controls
 */
export function ClientPipeline({
  side,
  rows,
  api,
  clientName,
  onRowsChange,
  drawerExtras,
  exportName = 'candidates',
}) {
  const [pile, setPile] = useState('all');
  const [query, setQuery] = useState('');
  const [role, setRole] = useState('');
  const [openId, setOpenId] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [bulkStage, setBulkStage] = useState('rejected');
  const [error, setError] = useState(null);
  const selectable = side === 'recruiter' && Boolean(api.bulkStage);

  const roles = useMemo(
    () => [...new Set(rows.map((r) => r.role_title).filter(Boolean))].sort(),
    [rows],
  );

  const counts = useMemo(() => {
    const out = {};
    for (const p of PILES) out[p.id] = rows.filter((r) => inPile(r, p.id)).length;
    return out;
  }, [rows]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return rows.filter(
      (r) =>
        inPile(r, pile) &&
        (!role || r.role_title === role) &&
        (!needle ||
          [r.full_name, r.current_title, r.current_company, r.location, r.latest_note?.body]
            .join(' ')
            .toLowerCase()
            .includes(needle)),
    );
  }, [rows, pile, query, role]);

  function replaceRow(updated) {
    onRowsChange(
      rows.map((r) =>
        r.id === updated.id
          ? {
              ...r,
              ...updated,
              note_count: updated.note_count ?? r.note_count,
              latest_note: updated.latest_note ?? null,
            }
          : r,
      ),
    );
  }

  async function quickStage(row, stage) {
    setError(null);
    const before = rows;
    onRowsChange(rows.map((r) => (r.id === row.id ? { ...r, stage } : r)));
    try {
      replaceRow(await api.setStage(row.id, stage));
    } catch (caught) {
      onRowsChange(before);
      setError(caught);
    }
  }

  async function applyBulk() {
    setError(null);
    try {
      const moved = await api.bulkStage([...selected], bulkStage);
      const byId = Object.fromEntries(moved.map((m) => [m.id, m]));
      onRowsChange(rows.map((r) => byId[r.id] ?? r));
      setSelected(new Set());
    } catch (caught) {
      setError(caught);
    }
  }

  const allVisibleSelected = visible.length > 0 && visible.every((r) => selected.has(r.id));
  function toggleAll() {
    const next = new Set(selected);
    if (allVisibleSelected) visible.forEach((r) => next.delete(r.id));
    else visible.forEach((r) => next.add(r.id));
    setSelected(next);
  }
  function toggle(id) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  return (
    <div className="stack">
      <div className="cp-piles" role="tablist" data-testid="cp-piles">
        {PILES.filter((p) => p.id === 'all' || p.id === pile || counts[p.id] > 0 || ['awaiting', 'in_process', 'first_round', 'rejected'].includes(p.id)).map((p) => (
          <button
            key={p.id}
            type="button"
            role="tab"
            aria-selected={pile === p.id}
            className={`cp-pile${pile === p.id ? ' active' : ''}`}
            onClick={() => setPile(p.id)}
            data-testid={`cp-pile-${p.id}`}
          >
            <span className="cp-pile-count">{counts[p.id]}</span>
            <span className="cp-pile-label">{p.label}</span>
          </button>
        ))}
      </div>

      <div className="spread cp-toolbar cp-inline-controls">
        <div className="row">
          <input
            type="text"
            placeholder="Search name, company, notes…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="cp-search"
            data-testid="cp-search"
          />
          {roles.length > 1 ? (
            <select value={role} onChange={(event) => setRole(event.target.value)}>
              <option value="">All roles</option>
              {roles.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          ) : null}
        </div>
        <div className="row">
          {selectable && selected.size ? (
            <span className="row cp-bulk" data-testid="cp-bulk">
              <span className="small">{selected.size} selected — move to</span>
              <select value={bulkStage} onChange={(event) => setBulkStage(event.target.value)}>
                {STAGES.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label}
                  </option>
                ))}
              </select>
              <button type="button" className="tiny" onClick={applyBulk}>
                Apply
              </button>
              <button type="button" className="tiny ghost" onClick={() => setSelected(new Set())}>
                Clear
              </button>
            </span>
          ) : null}
          <button
            type="button"
            className="tiny ghost"
            onClick={() => downloadCsv(`${exportName}.csv`, visible)}
            disabled={!visible.length}
          >
            Export CSV
          </button>
        </div>
      </div>

      <ErrorBox error={error} />

      <div className="card dgrid-wrap cp-grid-wrap">
        <table className="dgrid cp-grid" data-testid="cp-grid">
          <colgroup>
            {selectable ? <col style={{ width: 36 }} /> : null}
            <col style={{ width: '22%' }} />
            <col style={{ width: '24%' }} />
            {roles.length ? <col style={{ width: '12%' }} /> : null}
            <col style={{ width: 150 }} />
            <col style={{ width: 120 }} />
            <col />
          </colgroup>
          <thead>
            <tr>
              {selectable ? (
                <th>
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleAll}
                    aria-label="Select all shown"
                  />
                </th>
              ) : null}
              <th>Candidate</th>
              <th>Current role</th>
              {roles.length ? <th>Applied for</th> : null}
              <th>Stage</th>
              <th>Updated</th>
              <th>Latest note</th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 ? (
              <tr>
                <td colSpan={7} className="empty">
                  {rows.length ? 'Nobody in this pile.' : 'No candidates shared yet.'}
                </td>
              </tr>
            ) : (
              visible.map((row) => (
                <tr
                  key={row.id}
                  className="cp-row"
                  onClick={() => setOpenId(row.id)}
                  data-testid="cp-row"
                >
                  {selectable ? (
                    <td onClick={(event) => event.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={selected.has(row.id)}
                        onChange={() => toggle(row.id)}
                        aria-label={`Select ${row.full_name}`}
                      />
                    </td>
                  ) : null}
                  <td title={row.full_name}>
                    <span className="cp-name">{row.full_name || '—'}</span>{' '}
                    <LinkedInLink url={row.linkedin_url} />
                  </td>
                  <td className="muted" title={roleLine(row)}>
                    {roleLine(row)}
                  </td>
                  {roles.length ? (
                    <td className="muted" title={row.role_title}>
                      {row.role_title}
                    </td>
                  ) : null}
                  <td onClick={(event) => event.stopPropagation()}>
                    <StageSelect
                      value={row.stage}
                      onChange={(stage) => quickStage(row, stage)}
                      testId="cp-row-stage"
                    />
                  </td>
                  <td
                    className="muted small"
                    title={`${stageLabel(row.stage)} · ${new Date(row.stage_changed_at).toLocaleString()} by ${row.stage_changed_by_name}`}
                  >
                    {relativeTime(row.stage_changed_at)}
                    {side === 'recruiter' && row.stage_changed_by_kind === 'client' ? (
                      <span className="cp-by-client" title={`Moved by ${clientName}`}>
                        {' '}
                        · {clientName}
                      </span>
                    ) : null}
                  </td>
                  <td className="cp-note-cell" title={row.latest_note?.body ?? ''}>
                    {row.latest_note ? (
                      <>
                        {row.note_count > 1 ? (
                          <span className="badge cp-note-count">{row.note_count}</span>
                        ) : null}{' '}
                        <span className="muted">{row.latest_note.author_name}:</span>{' '}
                        {row.latest_note.body}
                      </>
                    ) : (
                      <span className="cp-add-note">+ Add note</span>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <div className="muted small">
        Showing {visible.length} of {rows.length}
      </div>

      {openId ? (
        <Drawer
          rowId={openId}
          side={side}
          api={api}
          clientName={clientName}
          onClose={() => setOpenId(null)}
          onChanged={replaceRow}
          extras={drawerExtras}
        />
      ) : null}
    </div>
  );
}
