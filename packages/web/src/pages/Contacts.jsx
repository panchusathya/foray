/**
 * Contacts — the CRM grid.
 *
 * Every piece of grid state lives in the URL rather than in useState: preset
 * views become plain links, the back button works, and a "going cold" list is
 * something you can paste to someone. The loader deps are then just the flat
 * param values, so useAsync keeps working unchanged.
 *
 * Sorting and paging are entirely server-side. At 200k rows the only client
 * that stays honest is one that asks for a hundred rows and renders a hundred
 * rows.
 *
 * Selection is *not* in the URL: it survives paging within a visit but is not
 * something you would want to send to someone, and a thousand ids in a query
 * string is not a link.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';

import { AddContactModal } from '../components/AddContactModal.jsx';
import { BulkBar } from '../components/BulkBar.jsx';
import { DiscoveredTray } from '../components/DiscoveredTray.jsx';
import { DuplicatesPanel } from '../components/DuplicatesPanel.jsx';
import { Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import {
  CONTACT_COLUMNS,
  CONTACT_VIEWS,
  DEFAULT_VIEW_ID,
  MAX_SELECT_ALL,
  PAGE_SIZE,
  countParams,
  resolveParams,
  viewById,
} from '../lib/contactViews.js';
import { useCurrentProject } from '../lib/project.jsx';
import {
  countOnPage,
  pageWindow,
  toggleOne,
  togglePage,
  toggleRange,
} from '../lib/selection.js';
import { useAsync } from '../lib/useAsync.js';

const MINUTE = 60000;
const DAY = 86400000;

/** Contacts key selection by candidate id; Sourcing keys by GitHub login. */
const byId = (row) => row.id;

/** "3d", "6w", "14mo" — dense enough for a grid cell, with the full stamp on hover. */
function relativeDate(value) {
  if (!value) return null;
  const then = new Date(value);
  const days = Math.floor((Date.now() - then.getTime()) / DAY);
  if (days <= 0) return 'today';
  if (days < 7) return `${days}d`;
  if (days < 60) return `${Math.floor(days / 7)}w`;
  if (days < 730) return `${Math.floor(days / 30)}mo`;
  return `${Math.floor(days / 365)}y`;
}

function DateCell({ value }) {
  const label = relativeDate(value);
  if (!label) return <span className="muted">—</span>;
  return <span title={new Date(value).toLocaleString()}>{label}</span>;
}

function Cell({ column, row }) {
  const history = row.history ?? {};
  switch (column) {
    case 'full_name':
      return (
        <Link to={`/contacts/${row.id}`} title={row.full_name}>
          {row.full_name}
        </Link>
      );
    case 'current_title':
      return row.current_title ? (
        <span title={row.current_title}>{row.current_title}</span>
      ) : (
        <span className="muted">—</span>
      );
    case 'current_company':
      return row.current_company ? (
        <span title={row.current_company}>{row.current_company}</span>
      ) : (
        <span className="muted">—</span>
      );
    case 'last_emailed_at':
      return <DateCell value={history.last_emailed_at} />;
    case 'last_reply_at':
      return <DateCell value={history.last_reply_at} />;
    case 'touches':
      return (
        <span title="emails sent / replies received">
          {history.outbound_count ?? 0}
          <span className="muted"> / </span>
          {history.inbound_count ?? 0}
        </span>
      );
    case 'emails':
      return row.emails?.length ? (
        <span className="mono muted" title={row.emails.join(', ')}>
          {row.emails[0]}
        </span>
      ) : (
        <span className="muted">—</span>
      );
    case 'created_at':
      return <DateCell value={row.created_at} />;
    default:
      return null;
  }
}

function ViewTabs({ activeId }) {
  return (
    <nav className="viewtabs" data-testid="view-tabs">
      {CONTACT_VIEWS.map((view) => (
        <Link
          key={view.id}
          to={`/contacts?view=${view.id}`}
          className={view.id === activeId ? 'active' : ''}
        >
          {view.label}
        </Link>
      ))}
    </nav>
  );
}

export function ContactsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  // The project list is already in context for the sidebar switcher; a
  // second fetch here would just be the same rows again.
  const { project, projects } = useCurrentProject();
  const navigate = useNavigate();

  const viewId = searchParams.get('view') ?? DEFAULT_VIEW_ID;
  const view = viewById(viewId);
  const sort = searchParams.get('sort') ?? view.sort;
  const dir = searchParams.get('dir') ?? view.dir;
  const page = Number(searchParams.get('page') ?? 0);
  const urlQuery = searchParams.get('q') ?? '';

  // The text box is the one thing that cannot live in the URL directly —
  // a param write per keystroke would push a history entry per keystroke.
  const [typed, setTyped] = useState(urlQuery);
  useEffect(() => setTyped(urlQuery), [urlQuery]);

  const [selected, setSelected] = useState(new Map());
  // Select-all-matching returns ids with no row bodies. Anything that needs
  // columns (export) has to know the difference.
  const [selectionIsIdsOnly, setSelectionIsIdsOnly] = useState(false);
  // Outcome of the last bulk action. Lives here rather than in BulkBar,
  // which unmounts the moment the selection clears.
  const [bulkNote, setBulkNote] = useState('');
  const [adding, setAdding] = useState(false);
  const lastIndex = useRef(null);

  function patchParams(changes, { resetPage = true } = {}) {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(changes)) {
      if (value === null || value === '') next.delete(key);
      else next.set(key, String(value));
    }
    if (resetPage) next.delete('page');
    setSearchParams(next);
  }

  useEffect(() => {
    if (typed === urlQuery) return undefined;
    const handle = setTimeout(() => patchParams({ q: typed }), 300);
    return () => clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typed]);

  // Rounded so a re-render mid-minute cannot shift a 90-day boundary and
  // re-fire the loader; the window only needs to be accurate to the minute.
  const nowKey = Math.floor(Date.now() / MINUTE);
  const now = useMemo(() => new Date(nowKey * MINUTE), [nowKey]);

  const listParams = resolveParams(viewId, { q: urlQuery, sort, dir, page, now });
  const totalParams = countParams(viewId, { q: urlQuery, now });
  const listKey = JSON.stringify(listParams);
  const totalKey = JSON.stringify(totalParams);

  // Hooks cannot be conditional, so the tray view short-circuits the loader
  // rather than skipping it: otherwise opening Discovered would also fetch a
  // page of candidates nobody is going to look at.
  const rows = useAsync(
    () =>
      view.panel
        ? Promise.resolve([])
        : api.contactsGrid({ ...listParams, include_history: true }),
    [listKey, view.panel],
  );
  const total = useAsync(
    () => (view.panel ? Promise.resolve(null) : api.contactsCount(totalParams)),
    [totalKey, view.panel],
  );
  const sequences = useAsync(() => api.listSequences(), []);

  const data = rows.data ?? [];
  const count = total.data?.count;
  const lastPage = count != null ? Math.max(0, Math.ceil(count / PAGE_SIZE) - 1) : null;
  const pageSelectedCount = countOnPage(selected, data, byId);

  // Changing what you are looking at should not silently keep acting on rows
  // you can no longer see.
  useEffect(() => {
    setSelected(new Map());
    setSelectionIsIdsOnly(false);
    setBulkNote('');
    lastIndex.current = null;
  }, [viewId, urlQuery]);

  function toggleSort(key) {
    if (sort !== key) return patchParams({ sort: key, dir: 'asc' });
    return patchParams({ sort: key, dir: dir === 'asc' ? 'desc' : 'asc' });
  }

  function onRowToggle(row, index, event) {
    if (event.shiftKey && lastIndex.current != null) {
      setSelected(toggleRange(selected, data, lastIndex.current, index, byId));
    } else {
      setSelected(toggleOne(selected, row, byId));
    }
    lastIndex.current = index;
    setSelectionIsIdsOnly(false);
  }

  async function selectAllMatching() {
    const { ids } = await api.contactIds({ ...totalParams, limit: MAX_SELECT_ALL });
    // Value is null: these came back as bare ids, with no row to export.
    setSelected(new Map(ids.map((id) => [id, null])));
    setSelectionIsIdsOnly(true);
  }

  function clearSelection(note = '') {
    setSelected(new Map());
    setSelectionIsIdsOnly(false);
    setBulkNote(typeof note === 'string' ? note : '');
    lastIndex.current = null;
  }

  return (
    <>
      <PageHeader
        title="Contacts"
        subtitle={
          count != null
            ? `${count.toLocaleString()} ${count === 1 ? 'person' : 'people'} · ${view.blurb}`
            : view.blurb
        }
        actions={
          <button type="button" onClick={() => setAdding(true)} data-testid="add-contact">
            Add contact
          </button>
        }
      />

      {adding ? (
        <AddContactModal
          onClose={() => setAdding(false)}
          onCreated={(created) => {
            setAdding(false);
            // Land on the record you just made: the next thing is usually a note.
            navigate(`/contacts/${created.id}`);
          }}
        />
      ) : null}

      <ViewTabs activeId={view.id} />

      {view.panel ? (
        view.panel === 'duplicates' ? <DuplicatesPanel /> : <DiscoveredTray />
      ) : (
      <div className="stack">
        <input
          type="text"
          placeholder="Search by name, title, or company…"
          value={typed}
          data-testid="contacts-search"
          onChange={(event) => setTyped(event.target.value)}
        />

        <BulkBar
          selected={selected}
          pageRows={data}
          pageSelectedCount={pageSelectedCount}
          onClear={clearSelection}
          onSelectAllMatching={selectAllMatching}
          matchingTotal={count}
          selectionIsIdsOnly={selectionIsIdsOnly}
          projects={projects ?? []}
          sequences={sequences.data ?? []}
          currentProjectId={project?.id ?? ''}
          onDone={(note) => {
            clearSelection(note);
            rows.reload();
            total.reload();
          }}
        />

        {bulkNote ? (
          <div className="card small" data-testid="bulk-result">
            {bulkNote}
          </div>
        ) : null}

        <ErrorBox error={rows.error} />

        {rows.loading ? (
          <Loading label="Loading contacts…" />
        ) : data.length ? (
          <div className="card dgrid-wrap">
            <table className="dgrid" data-testid="contacts-grid">
              <colgroup>
                <col style={{ width: 34 }} />
                {view.columns.map((key) => (
                  <col key={key} style={{ width: CONTACT_COLUMNS[key].width }} />
                ))}
              </colgroup>
              <thead>
                <tr>
                  <th className="pick">
                    <input
                      type="checkbox"
                      aria-label="Select page"
                      data-testid="select-page"
                      checked={data.length > 0 && pageSelectedCount === data.length}
                      onChange={() => {
                        setSelected(togglePage(selected, data, byId));
                        setSelectionIsIdsOnly(false);
                      }}
                    />
                  </th>
                  {view.columns.map((key) => {
                    const column = CONTACT_COLUMNS[key];
                    const active = column.sort && column.sort === sort;
                    return (
                      <th
                        key={key}
                        className={column.numeric ? 'num' : undefined}
                        aria-sort={
                          active ? (dir === 'asc' ? 'ascending' : 'descending') : undefined
                        }
                      >
                        {column.sort ? (
                          <button
                            type="button"
                            className="sort"
                            data-testid={`sort-${column.sort}`}
                            onClick={() => toggleSort(column.sort)}
                          >
                            {column.label}
                            {active ? (dir === 'asc' ? ' ▲' : ' ▼') : ''}
                          </button>
                        ) : (
                          column.label
                        )}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {data.map((row, index) => {
                  const isSelected = selected.has(row.id);
                  return (
                    <tr
                      key={row.id}
                      data-testid="contact-row"
                      data-id={row.id}
                      className={isSelected ? 'selected' : undefined}
                    >
                      <td className="pick">
                        <input
                          type="checkbox"
                          aria-label={`Select ${row.full_name}`}
                          checked={isSelected}
                          // Shift-click is swallowed by the label unless the
                          // click is handled directly; onChange stays a noop.
                          onClick={(event) => onRowToggle(row, index, event)}
                          onChange={() => {}}
                        />
                      </td>
                      {view.columns.map((key) => (
                        <td
                          key={key}
                          className={CONTACT_COLUMNS[key].numeric ? 'num' : undefined}
                        >
                          <Cell column={key} row={row} />
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>{urlQuery ? `No contacts match “${urlQuery}”.` : view.empty}</Empty>
        )}

        {(page > 0 || data.length === PAGE_SIZE) && (
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <button
              type="button"
              disabled={page === 0}
              onClick={() => patchParams({ page: page - 1 }, { resetPage: false })}
            >
              Previous
            </button>
            <div className="row" style={{ gap: 4 }}>
              {lastPage != null &&
                pageWindow(page, lastPage + 1).map((n) => (
                  <button
                    key={n}
                    type="button"
                    className={n === page ? 'tiny' : 'tiny ghost'}
                    onClick={() => patchParams({ page: n }, { resetPage: false })}
                  >
                    {n + 1}
                  </button>
                ))}
            </div>
            <button
              type="button"
              disabled={lastPage != null ? page >= lastPage : data.length < PAGE_SIZE}
              onClick={() => patchParams({ page: page + 1 }, { resetPage: false })}
            >
              Next
            </button>
          </div>
        )}
      </div>
      )}
    </>
  );
}
