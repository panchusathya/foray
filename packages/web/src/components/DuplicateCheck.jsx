/**
 * Check a list against the CRM without importing it.
 *
 * Drop in any CSV or workbook -- names, LinkedIn URLs, addresses, whatever
 * columns it happens to have -- and get the same file back with everybody we
 * already know taken out. Nothing is written: the file is parsed here, and the
 * only server call is the read-only POST /v1/imports/known the importer uses.
 *
 * Every match starts out removed, but each one can be kept: a name-and-company
 * match can be two different people, and the recruiter is the one who knows.
 */

import { useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../lib/api.js';
import { parseTable } from '../lib/csvImport.js';
import {
  AUTO,
  NONE,
  SPLIT_NAME,
  applyMatches,
  buildCheckRows,
  chunkPeople,
  defaultMapping,
  guessColumns,
  lookupPeople,
  outputName,
  summarize,
  writeTable,
} from '../lib/dupeCheck.js';
import { newestOnly } from '../lib/race.js';
import { Badge, Empty, ErrorBox, Stat } from './common.jsx';

const MATCH_TONE = { email: 'strong', linkedin: 'strong', name_and_company: 'partial' };
const MATCH_LABEL = {
  email: 'same email',
  linkedin: 'same LinkedIn',
  name_and_company: 'same name & company',
};

function saveText(text, filename) {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function ColumnPicker({ label, value, onChange, header, extra = [], testId }) {
  return (
    <label className="small muted" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
      {label}
      <select value={value} onChange={(event) => onChange(event.target.value)} data-testid={testId}>
        {extra.map(([optionValue, optionLabel]) => (
          <option key={optionValue} value={optionValue}>
            {optionLabel}
          </option>
        ))}
        <option value={NONE}>— not used —</option>
        {header.map((name, index) => (
          // eslint-disable-next-line react/no-array-index-key -- a header can repeat
          <option key={index} value={String(index)}>
            {name || `Column ${index + 1}`}
          </option>
        ))}
      </select>
    </label>
  );
}

function autoLabel(indices, header, what) {
  if (!indices.length) return `Auto — any cell that is ${what}`;
  const names = indices.map((index) => `“${header[index] || `Column ${index + 1}`}”`);
  return `Auto — ${names.join(', ')}`;
}

export function DuplicateCheck() {
  const [fileName, setFileName] = useState('');
  const [table, setTable] = useState(null);
  const [guess, setGuess] = useState(null);
  const [mapping, setMapping] = useState(null);
  const [rows, setRows] = useState([]);
  const [removed, setRemoved] = useState(() => new Set());
  const [checked, setChecked] = useState(false);
  const [checking, setChecking] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);
  // A second file, or a changed column, while a check is in flight: the
  // slower answer must not flag rows it was never asked about.
  const startCheck = useRef(newestOnly()).current;

  const summary = useMemo(() => summarize(rows, removed), [rows, removed]);
  const visible = showAll ? rows : rows.filter((row) => row.match);

  async function onFile(file) {
    if (!file) return;
    // Retire any check still running for the previous file before anything
    // else: a workbook takes a round trip to convert, and that is long enough
    // for the old answer to land on the new table.
    startCheck();
    setChecking(false);
    setError(null);
    setNote('');
    setFileName(file.name);
    setTable(null);
    setRows([]);
    setRemoved(new Set());
    setChecked(false);

    let text = '';
    if (/\.xlsx?m?$/i.test(file.name)) {
      // Same one-way conversion the importer uses; that route stores nothing.
      setBusy(true);
      try {
        const converted = await api.workbookToCsv(file);
        text = converted.delimited ?? '';
        if ((converted.sheets ?? []).length > 1) {
          setNote(
            `Read the sheet “${converted.sheet}”. This workbook also has: `
              + `${converted.sheets.filter((s) => s !== converted.sheet).join(', ')}.`,
          );
        }
      } catch (caught) {
        setError(caught);
        return;
      } finally {
        setBusy(false);
      }
    } else {
      try {
        text = await file.text();
      } catch {
        setError(new Error('Could not read that file.'));
        return;
      }
    }

    const parsed = parseTable(text);
    if (!parsed.cells.length) {
      setError(new Error('No rows in that file — the first line should be the column names.'));
      return;
    }
    const guessed = guessColumns(parsed.header);
    const initial = defaultMapping(guessed);
    setTable(parsed);
    setGuess(guessed);
    setMapping(initial);
    check(parsed, guessed, initial);
  }

  async function check(parsed, guessed, nextMapping) {
    const built = buildCheckRows(parsed.cells, nextMapping, guessed);
    setRows(built);
    setChecked(false);
    setTruncated(false);
    const people = lookupPeople(built);
    const isNewest = startCheck();
    if (!people.length) {
      setRemoved(new Set());
      setChecking(false);
      setChecked(true);
      return;
    }
    setChecking(true);
    setError(null);
    try {
      const matches = [];
      let anyTruncated = false;
      for (const batch of chunkPeople(people)) {
        const answer = await api.knownContacts({ people: batch });
        if (!isNewest()) return;
        matches.push(...(answer?.matches ?? []));
        anyTruncated = anyTruncated || Boolean(answer?.truncated);
      }
      const flagged = applyMatches(built, matches);
      setRows(flagged);
      setTruncated(anyTruncated);
      // Every match starts out removed -- the file is for the people we do
      // not have -- and the table is where a weak one gets put back.
      setRemoved(new Set(flagged.filter((row) => row.match).map((row) => row.key)));
      setChecked(true);
    } catch (caught) {
      if (isNewest()) setError(caught);
    } finally {
      if (isNewest()) setChecking(false);
    }
  }

  function remap(role, value) {
    const next = { ...mapping, [role]: value };
    setMapping(next);
    check(table, guess, next);
  }

  function toggle(key) {
    setRemoved((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function download(which) {
    const keep = which === 'clean'
      ? rows.filter((row) => !removed.has(row.key))
      : rows.filter((row) => removed.has(row.key));
    const text = writeTable({
      header: table.header,
      rows: keep.map((row) => row.cells),
      delimiter: table.delimiter,
      bom: table.bom,
    });
    saveText(text, outputName(fileName, which === 'clean' ? 'deduped' : 'duplicates', table.delimiter));
  }

  function reset() {
    setFileName('');
    setTable(null);
    setGuess(null);
    setMapping(null);
    setRows([]);
    setRemoved(new Set());
    setChecked(false);
    setChecking(false);
    setTruncated(false);
    setNote('');
    setError(null);
    startCheck();
    if (inputRef.current) inputRef.current.value = '';
  }

  const nameOptions = guess && guess.first >= 0 && guess.last >= 0
    ? [[SPLIT_NAME, `First + last (“${table.header[guess.first]}” + “${table.header[guess.last]}”)`]]
    : [];

  return (
    <div className="stack" data-testid="dupe-check">
      <div className="card stack">
        <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <input
            ref={inputRef}
            type="file"
            accept=".csv,.tsv,.txt,.xlsx,.xlsm,text/csv"
            data-testid="dupe-file"
            onChange={(event) => onFile(event.target.files?.[0])}
          />
          {fileName ? (
            <button type="button" className="tiny" onClick={reset}>
              Clear
            </button>
          ) : null}
        </div>
        <div className="small muted">
          Any list with LinkedIn URLs, emails, or names and companies. It is checked against
          your contacts and you get the same file back without the people already in them.
          Nothing is imported.
        </div>
        {busy ? <div className="small muted">Reading the workbook…</div> : null}
        {note ? <div className="small muted">{note}</div> : null}
      </div>

      <ErrorBox error={error} />

      {table && mapping ? (
        <>
          <div className="card stack">
            <strong className="small">Matching on</strong>
            <div className="row wrap" style={{ gap: 14 }}>
              <ColumnPicker
                label="LinkedIn"
                value={mapping.linkedin}
                onChange={(value) => remap('linkedin', value)}
                header={table.header}
                extra={[[AUTO, autoLabel(guess.linkedin, table.header, 'a LinkedIn URL')]]}
                testId="dupe-map-linkedin"
              />
              <ColumnPicker
                label="Email"
                value={mapping.email}
                onChange={(value) => remap('email', value)}
                header={table.header}
                extra={[[AUTO, autoLabel(guess.emails, table.header, 'an address')]]}
                testId="dupe-map-email"
              />
              <ColumnPicker
                label="Name"
                value={mapping.name}
                onChange={(value) => remap('name', value)}
                header={table.header}
                extra={nameOptions}
                testId="dupe-map-name"
              />
              <ColumnPicker
                label="Company"
                value={mapping.company}
                onChange={(value) => remap('company', value)}
                header={table.header}
                testId="dupe-map-company"
              />
            </div>
            <div className="small muted">
              A name only counts alongside a company — two people can share a name, so a name on
              its own is never treated as a match.
            </div>
          </div>

          <div className="card stack">
            <div className="row" style={{ gap: 24, flexWrap: 'wrap' }}>
              <Stat value={summary.total} label="rows in file" />
              <Stat value={checking ? '…' : summary.matched} label="already in contacts" />
              <Stat value={checking ? '…' : summary.keeping} label="in the cleaned file" />
            </div>
            {checking ? (
              <div className="small muted" data-testid="dupe-checking">
                Checking {summary.total - summary.unchecked} rows against your contacts…
              </div>
            ) : null}
            {checked && summary.unchecked ? (
              <div className="small muted" data-testid="dupe-unchecked">
                {summary.unchecked} row{summary.unchecked === 1 ? ' has' : 's have'} nothing to
                match on — no LinkedIn, no email, and no name with a company — so{' '}
                {summary.unchecked === 1 ? 'it stays' : 'they stay'} in the cleaned file. Check
                the columns above if that is more than you expected.
              </div>
            ) : null}
            {checked && summary.weak ? (
              <div className="small muted">
                {summary.weak} match{summary.weak === 1 ? ' is' : 'es are'} on name and company
                alone, which can be two different people. Untick any you want to keep.
              </div>
            ) : null}
            {truncated ? (
              <div className="small muted">
                This workspace has more contacts than the LinkedIn check scans, so a row with no
                match is not proof it is new. Email matches are unaffected.
              </div>
            ) : null}
            <div className="row wrap" style={{ gap: 10 }}>
              <button
                type="button"
                className="primary"
                disabled={!checked || checking}
                onClick={() => download('clean')}
                data-testid="dupe-download"
              >
                Download {summary.keeping.toLocaleString()} without duplicates
              </button>
              <button
                type="button"
                className="ghost"
                disabled={!checked || checking || !summary.removing}
                onClick={() => download('removed')}
                data-testid="dupe-download-removed"
              >
                Download the {summary.removing.toLocaleString()} removed
              </button>
            </div>
          </div>

          {checked ? (
            <div className="card stack">
              <label className="small muted" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <input
                  type="checkbox"
                  checked={showAll}
                  onChange={(event) => setShowAll(event.target.checked)}
                />
                Show every row, not just the matches
              </label>
              {visible.length ? (
                <table className="jobs-table">
                  <thead>
                    <tr>
                      <th style={{ width: 70 }}>Remove</th>
                      <th style={{ width: 50 }}>Row</th>
                      <th>Name</th>
                      <th>Company</th>
                      <th>Matched on</th>
                      <th>Already in contacts as</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((row) => (
                      <tr key={row.key} data-testid="dupe-row">
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Remove row ${row.index + 2}`}
                            checked={removed.has(row.key)}
                            onChange={() => toggle(row.key)}
                          />
                        </td>
                        {/* +2: one for the header line, one for counting from 1,
                            so this is the line number in their spreadsheet. */}
                        <td className="small muted">{row.index + 2}</td>
                        <td>{row.identity.full_name || <span className="small muted">—</span>}</td>
                        <td className="small">{row.identity.company || '—'}</td>
                        <td className="small">
                          {row.match ? (
                            <span title={row.match.reason}>
                              <Badge tone={MATCH_TONE[row.match.matchedOn] ?? ''}>
                                {MATCH_LABEL[row.match.matchedOn] ?? row.match.matchedOn}
                              </Badge>
                            </span>
                          ) : row.checkable ? (
                            <span className="muted">new</span>
                          ) : (
                            <span className="muted">nothing to match on</span>
                          )}
                        </td>
                        <td className="small">
                          {row.match ? (
                            <Link to={`/contacts/${row.match.candidateId}`}>
                              {row.match.fullName || 'existing contact'}
                            </Link>
                          ) : (
                            '—'
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <Empty>Nobody in this file is in your contacts.</Empty>
              )}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
