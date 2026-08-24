import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, PageHeader, Stat } from '../components/common.jsx';
import { api } from '../lib/api.js';
import {
  buildImportRows,
  chunk,
  describeImport,
  detectColumns,
  hasEmailColumn,
  parseDelimited,
  summarizeImport,
} from '../lib/csvImport.js';

// Import candidates from a Juicebox CSV export and drop them straight into a
// sequence. The whole point is that the sequencing side is unchanged: this
// screen ends in the same POST /v1/sequences/{id}/enroll that the Contacts and
// Sourcing screens call, so a CSV-sourced enrolment and a search-sourced one
// are the same row afterwards.
//
// The file is parsed in the browser and nothing is written until the recruiter
// presses the button, which is what makes "take however many I want" possible:
// the whole file is on screen, with the address we would actually mail shown
// per row, before any of it becomes a record.

const VERIFICATION_TONE = {
  deliverable: 'good',
  risky: 'partial',
  undeliverable: 'bad',
};

function EmailCell({ row }) {
  if (row.primaryEmail) {
    return (
      <div className="cell-stack">
        <span className="mono small">{row.primaryEmail}</span>
        <span className="cell-sub small muted">
          {row.primaryKind}
          {row.verification ? (
            <>
              {' · '}
              <Badge tone={VERIFICATION_TONE[row.verification] ?? ''}>{row.verification}</Badge>
            </>
          ) : (
            ' · unverified'
          )}
        </span>
      </div>
    );
  }
  return (
    <div className="cell-stack">
      <span className="small muted">—</span>
      <span className="cell-sub small muted">{row.blocked}</span>
    </div>
  );
}

export function ImportPage() {
  const [fileName, setFileName] = useState('');
  const [parseError, setParseError] = useState('');
  const [columns, setColumns] = useState({});
  const [importRows, setImportRows] = useState([]);
  const [chosen, setChosen] = useState(() => new Set());
  const [showAll, setShowAll] = useState(false);

  const [sequences, setSequences] = useState([]);
  const [sequenceId, setSequenceId] = useState('');
  const [sendNow, setSendNow] = useState(false);

  const [busy, setBusy] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState(null);
  const inputRef = useRef(null);

  useEffect(() => {
    let live = true;
    api
      .listSequences()
      .then((rows) => {
        if (!live) return;
        // Only sequences that can actually send. Offering an archived one is a
        // way to enrol somebody into nothing and think it worked.
        setSequences((rows ?? []).filter((s) => s.status !== 'archived'));
      })
      .catch(() => {
        /* the picker just stays empty; importing without enrolling still works */
      });
    return () => {
      live = false;
    };
  }, []);

  const summary = useMemo(() => summarizeImport(importRows), [importRows]);
  const emailColumnPresent = hasEmailColumn(columns);
  const visibleRows = showAll ? importRows : importRows.filter((r) => r.sendable);
  const chosenRows = importRows.filter((r) => chosen.has(r.key) && r.sendable);

  async function onFile(file) {
    if (!file) return;
    setError(null);
    setStatus('');
    setParseError('');
    setFileName(file.name);

    if (/\.xlsx?$/i.test(file.name)) {
      setImportRows([]);
      setColumns({});
      setParseError(
        'That is an Excel workbook, which this screen cannot read. Open it and '
          + '"Save as" CSV — or re-download from Juicebox, which exports CSV natively.',
      );
      return;
    }

    let text = '';
    try {
      text = await file.text();
    } catch {
      setParseError('Could not read that file.');
      return;
    }

    const { headers, rows } = parseDelimited(text);
    if (!rows.length) {
      setImportRows([]);
      setColumns({});
      setParseError('No rows in that file — is it the right export?');
      return;
    }
    const detected = detectColumns(headers);
    const built = buildImportRows(rows, detected);
    setColumns(detected);
    setImportRows(built);
    // Pre-select exactly the rows that can be mailed. Everything else is
    // visible but off, so the default action is never the surprising one.
    setChosen(new Set(built.filter((r) => r.sendable).map((r) => r.key)));
    setShowAll(false);
  }

  function toggle(key) {
    setChosen((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleAllVisible(on) {
    setChosen((previous) => {
      const next = new Set(previous);
      for (const row of visibleRows) {
        if (!row.sendable) continue;
        if (on) next.add(row.key);
        else next.delete(row.key);
      }
      return next;
    });
  }

  function reset() {
    setFileName('');
    setImportRows([]);
    setColumns({});
    setChosen(new Set());
    setParseError('');
    setStatus('');
    setError(null);
    if (inputRef.current) inputRef.current.value = '';
  }

  async function run({ enroll }) {
    if (!chosenRows.length) return;
    if (enroll && !sequenceId) return;
    setBusy(enroll ? 'enrolling' : 'importing');
    setError(null);
    setStatus('');
    try {
      // Chunked because /v1/candidates/bulk caps a request at 5000, and the ids
      // are collected across chunks so the enrol call covers the whole file.
      const ids = [];
      let created = 0;
      let updated = 0;
      for (const batch of chunk(chosenRows.map((r) => r.payload))) {
        const result = await api.bulkCandidates({ candidates: batch });
        ids.push(...(result.ids ?? []));
        created += result.created ?? 0;
        updated += result.updated ?? 0;
      }

      const imported = `${created} new, ${updated} updated`;
      if (!enroll) {
        setStatus(`Imported ${ids.length} into contacts (${imported}).`);
        setBusy('');
        return;
      }

      const enrollments = await api.enroll(sequenceId, {
        candidate_ids: ids,
        start_immediately: true,
        send_now: sendNow,
      });
      const name = sequences.find((s) => s.id === sequenceId)?.name ?? 'the sequence';
      setStatus(
        `Imported ${ids.length} (${imported}) and enrolled `
          + `${enrollments?.length ?? ids.length} in ${name}`
          + `${sendNow ? ' — the first email is going out now.' : '.'}`,
      );
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="stack">
      <PageHeader
        title="Import from CSV"
        subtitle="Upload a Juicebox export, pick who to take, and enroll them in a sequence."
      />

      <div className="card stack">
        <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <input
            ref={inputRef}
            type="file"
            accept=".csv,.tsv,.txt,text/csv"
            data-testid="import-file"
            onChange={(event) => onFile(event.target.files?.[0])}
          />
          {fileName ? (
            <button type="button" className="tiny" onClick={reset}>
              Clear
            </button>
          ) : null}
        </div>
        <div className="small muted">
          A Juicebox export with the contact columns ticked — “Personal Email” and “Work
          Email”. An export without them imports profiles nobody can be mailed.
        </div>
        {parseError ? <Empty>{parseError}</Empty> : null}
      </div>

      <ErrorBox error={error} />

      {importRows.length ? (
        <>
          <div className="card stack">
            <div className="row" style={{ gap: 24, flexWrap: 'wrap' }}>
              <Stat value={summary.total} label="rows in file" />
              <Stat value={summary.sendable} label="can be mailed" />
              <Stat value={chosenRows.length} label="selected" />
            </div>
            <div className="small muted" data-testid="import-summary">
              {describeImport(summary)}
            </div>
            {!emailColumnPresent ? (
              <Empty>
                This export has no email columns at all, so nobody in it can be sequenced.
                Re-export from Juicebox with “Personal Email” ticked.
              </Empty>
            ) : null}
            {summary.undeliverableOnly ? (
              <div className="small muted">
                {summary.undeliverableOnly} row
                {summary.undeliverableOnly === 1 ? '' : 's'} had only an address Juicebox marked
                undeliverable. Those are left out — mailing a known-bad address costs sending
                reputation for every other candidate.
              </div>
            ) : null}
          </div>

          <div className="card stack">
            <div className="row" style={{ gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              <label className="small muted" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <select
                  value={sequenceId}
                  onChange={(event) => setSequenceId(event.target.value)}
                  data-testid="import-sequence"
                >
                  <option value="">Choose a sequence…</option>
                  {sequences.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                      {s.status === 'paused' ? ' (paused)' : ''}
                    </option>
                  ))}
                </select>
              </label>
              <label className="small muted" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <input
                  type="checkbox"
                  checked={sendNow}
                  onChange={(event) => setSendNow(event.target.checked)}
                />
                Send the first email immediately
              </label>
              <button
                type="button"
                className="tiny primary"
                disabled={!chosenRows.length || !sequenceId || Boolean(busy)}
                onClick={() => run({ enroll: true })}
                data-testid="import-enroll"
              >
                {busy === 'enrolling'
                  ? 'Enrolling…'
                  : `Import ${chosenRows.length} and enroll`}
              </button>
              <button
                type="button"
                className="tiny"
                disabled={!chosenRows.length || Boolean(busy)}
                onClick={() => run({ enroll: false })}
              >
                {busy === 'importing' ? 'Importing…' : 'Import only'}
              </button>
            </div>
            {!sequences.length ? (
              <div className="small muted">
                No sequences yet — <Link to="/sequences">create one</Link>, or import to
                contacts now and enroll later.
              </div>
            ) : null}
            {status ? (
              <div className="small" data-testid="import-status">
                {status}
              </div>
            ) : null}
          </div>

          <div className="card stack">
            <div className="row" style={{ gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              <label className="small muted" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <input
                  type="checkbox"
                  checked={
                    visibleRows.some((r) => r.sendable)
                    && visibleRows.filter((r) => r.sendable).every((r) => chosen.has(r.key))
                  }
                  onChange={(event) => toggleAllVisible(event.target.checked)}
                />
                Select all shown
              </label>
              <label className="small muted" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <input
                  type="checkbox"
                  checked={showAll}
                  onChange={(event) => setShowAll(event.target.checked)}
                />
                Show the {summary.total - summary.sendable} row
                {summary.total - summary.sendable === 1 ? '' : 's'} that cannot be mailed
              </label>
            </div>

            <table className="jobs-table">
              <thead>
                <tr>
                  <th style={{ width: 32 }} aria-label="select" />
                  <th>Name</th>
                  <th>Title</th>
                  <th>Company</th>
                  <th>Email we would use</th>
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((row) => (
                  <tr key={row.key} data-testid="import-row">
                    <td>
                      <input
                        type="checkbox"
                        checked={chosen.has(row.key)}
                        disabled={!row.sendable}
                        onChange={() => toggle(row.key)}
                      />
                    </td>
                    <td>{row.name || <span className="small muted">— no name —</span>}</td>
                    <td className="small">{row.title || '—'}</td>
                    <td className="small">{row.company || '—'}</td>
                    <td>
                      <EmailCell row={row} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!visibleRows.length ? (
              <Empty>Nothing in this file can be mailed.</Empty>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}
