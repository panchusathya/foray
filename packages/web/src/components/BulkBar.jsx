/**
 * Bulk actions for the Contacts grid.
 *
 * Only rendered when something is selected — on a dense grid a permanently
 * visible action bar is just a row of disabled buttons taking up the space the
 * table wants. Select-page therefore lives in the table header, not here: a
 * control that only appears once you have already selected something cannot
 * be how you start selecting.
 *
 * Export writes only the rows currently loaded. "Select all matching" fetches
 * ids and nothing else, so there are no columns to write and offering export
 * on that path would silently produce a different file than the grid shows.
 */

import { useState } from 'react';

import { api } from '../lib/api.js';
import { MAX_SELECT_ALL } from '../lib/contactViews.js';
import { ErrorBox } from './common.jsx';

const CSV_COLUMNS = [
  ['Name', (row) => row.full_name],
  ['Title', (row) => row.current_title],
  ['Company', (row) => row.current_company],
  ['Email', (row) => (row.emails ?? []).join(' ')],
  ['Last contacted', (row) => row.history?.last_emailed_at ?? ''],
  ['Last reply', (row) => row.history?.last_reply_at ?? ''],
  ['Sent', (row) => row.history?.outbound_count ?? 0],
  ['Replies', (row) => row.history?.inbound_count ?? 0],
];

function csvCell(value) {
  const text = value == null ? '' : String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCsv(rows) {
  const lines = [CSV_COLUMNS.map(([label]) => label).join(',')];
  for (const row of rows) {
    lines.push(CSV_COLUMNS.map(([, read]) => csvCell(read(row))).join(','));
  }
  return lines.join('\n');
}

export function BulkBar({
  selected,
  pageRows,
  pageSelectedCount,
  onClear,
  onSelectAllMatching,
  matchingTotal,
  selectionIsIdsOnly,
  projects,
  sequences,
  currentProjectId,
  onDone,
}) {
  const [projectId, setProjectId] = useState(currentProjectId ?? '');
  const [sequenceId, setSequenceId] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState(null);

  const count = selected.size;
  if (!count) return null;

  const ids = [...selected.keys()];
  const rows = [...selected.values()].filter(Boolean);
  const canOfferSelectAll =
    matchingTotal != null && matchingTotal > count && matchingTotal <= MAX_SELECT_ALL;

  async function run(label, work) {
    setBusy(label);
    setError(null);
    try {
      // The outcome is reported upward, not rendered here: onDone clears the
      // selection, which unmounts this bar and would take the confirmation
      // with it -- leaving no sign the action had happened at all.
      const message = await work();
      onDone?.(message);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy('');
    }
  }

  function addToProject() {
    if (!projectId) return;
    run('project', async () => {
      await api.addToProject(projectId, { candidate_ids: ids, search_id: null });
      const name = projects.find((p) => p.id === projectId)?.name ?? 'the project';
      return `Added ${count} to ${name}.`;
    });
  }

  function enrol() {
    if (!sequenceId) return;
    // Same wording as the sequence editor's enrol panel, so the two surfaces
    // do not describe the same outcome differently.
    const withoutEmail = rows.filter((row) => !(row.emails ?? []).length);
    if (selectionIsIdsOnly || withoutEmail.length) {
      const detail = selectionIsIdsOnly
        ? 'Some of these may have no email yet.'
        : `${withoutEmail.length} of ${count} have no email yet — they wait until one is found.`;
      if (!window.confirm(`Enrol ${count} ${count === 1 ? 'person' : 'people'}? ${detail}`)) {
        return;
      }
    }
    run('enrol', async () => {
      await api.enroll(sequenceId, { candidate_ids: ids, project_id: projectId || null });
      return `Enrolled ${count}.`;
    });
  }

  function exportCsv() {
    const blob = new Blob([toCsv(rows)], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `contacts-${rows.length}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="card stack" data-testid="bulk-bar" style={{ gap: 10 }}>
      <div className="row spread wrap">
        <div className="row" style={{ gap: 10 }}>
          {/* Badge does not forward props, and this needs a testid. */}
          <span className="badge accent" data-testid="bulk-count">
            {count} selected
          </span>
          {pageSelectedCount < count ? (
            <span className="muted small">{pageSelectedCount} on this page</span>
          ) : null}
          <button type="button" className="tiny ghost" onClick={onClear}>
            Clear
          </button>
        </div>

        <div className="row wrap" style={{ gap: 8, justifyContent: 'flex-end' }}>
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            <option value="">Add to project…</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={!projectId || !!busy}
            onClick={addToProject}
            data-testid="bulk-add-to-project"
          >
            {busy === 'project' ? 'Adding…' : 'Add'}
          </button>

          <select value={sequenceId} onChange={(e) => setSequenceId(e.target.value)}>
            <option value="">Enrol in sequence…</option>
            {sequences.map((sequence) => (
              <option key={sequence.id} value={sequence.id}>
                {sequence.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={!sequenceId || !!busy}
            onClick={enrol}
            data-testid="bulk-enrol"
          >
            {busy === 'enrol' ? 'Enrolling…' : 'Enrol'}
          </button>

          <button
            type="button"
            className="ghost"
            disabled={selectionIsIdsOnly}
            title={
              selectionIsIdsOnly
                ? 'Select-all fetches ids only — there are no columns to export.'
                : 'Download the selected rows as CSV'
            }
            onClick={exportCsv}
            data-testid="bulk-export"
          >
            Export CSV
          </button>
        </div>
      </div>

      {canOfferSelectAll ? (
        <button
          type="button"
          className="tiny ghost"
          onClick={onSelectAllMatching}
          data-testid="select-all-matching"
        >
          Select all {matchingTotal} matching
        </button>
      ) : null}
      {matchingTotal != null && matchingTotal > MAX_SELECT_ALL && matchingTotal > count ? (
        <span className="muted small">
          {matchingTotal.toLocaleString()} match — too many to select at once. Narrow the view
          with a filter or search first.
        </span>
      ) : null}

      <ErrorBox error={error} />
    </div>
  );
}
