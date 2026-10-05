import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { DuplicateCheck } from '../components/DuplicateCheck.jsx';
import { Badge, Empty, ErrorBox, PageHeader, Stat, ViewTabs } from '../components/common.jsx';
import { api } from '../lib/api.js';
import {
  IMPORT_STATE_KEY,
  applyKnownMatches,
  buildImportRows,
  chunk,
  describeImport,
  detectColumns,
  hasEmailColumn,
  knownLookupPayload,
  parseDelimited,
  pruneChosen,
  readImportDraft,
  summarizeImport,
  withoutDuplicates,
} from '../lib/csvImport.js';
import { newestOnly } from '../lib/race.js';

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

// The badge tones the stylesheet actually defines. This used to say
// good/partial/bad, and .badge.good and .badge.bad do not exist -- so only
// "risky" ever rendered styled.
const VERIFICATION_TONE = {
  deliverable: 'strong',
  risky: 'partial',
  undeliverable: 'miss',
};

// Strongest first, same order as duplicates.MATCHED_ON on the server. A
// name-and-company match is the weakest of the three and the one worth reading
// sceptically, so it is toned differently rather than presented as certain.
const MATCH_TONE = {
  email: 'strong',
  linkedin: 'strong',
  name_and_company: 'partial',
};

const MATCH_LABEL = {
  email: 'same email',
  linkedin: 'same LinkedIn',
  name_and_company: 'same name & company',
};

function DuplicateCell({ row }) {
  if (!row.duplicateOf) return <span className="small muted">—</span>;
  const { candidateId, fullName, matchedOn, reason } = row.duplicateOf;
  return (
    <div className="cell-stack" data-testid="import-duplicate">
      <Link to={`/contacts/${candidateId}`} className="small">
        {fullName || 'existing contact'}
      </Link>
      <span className="cell-sub small muted" title={reason}>
        <Badge tone={MATCH_TONE[matchedOn] ?? ''}>{MATCH_LABEL[matchedOn] ?? matchedOn}</Badge>
      </span>
    </div>
  );
}

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

const IMPORT_TABS = [
  {
    id: 'import',
    label: 'Import to sequence',
    subtitle: 'Upload a sourcing export, pick who to take, and enroll them in a sequence.',
  },
  {
    id: 'duplicates',
    label: 'Check for duplicates',
    subtitle:
      'Check any list against your contacts and download it without the people already in them. '
      + 'Nothing is imported.',
  },
];

export function ImportPage() {
  const [searchParams] = useSearchParams();
  const tab = IMPORT_TABS.find((t) => t.id === searchParams.get('view')) ?? IMPORT_TABS[0];
  return (
    <div className="stack">
      <PageHeader title="Import from CSV" subtitle={tab.subtitle} />
      <ViewTabs views={IMPORT_TABS} activeId={tab.id} basePath="/import" />
      {/* Both stay mounted, hidden rather than unmounted: flipping to the other
          tab and back must not throw away a file somebody has been working
          through. */}
      <div hidden={tab.id !== 'import'}>
        <ImportToSequence />
      </div>
      <div hidden={tab.id !== 'duplicates'}>
        <DuplicateCheck />
      </div>
    </div>
  );
}

function ImportToSequence() {
  const [fileName, setFileName] = useState('');
  const [parseError, setParseError] = useState('');
  const [columns, setColumns] = useState({});
  const [importRows, setImportRows] = useState([]);
  const [chosen, setChosen] = useState(() => new Set());
  const [showAll, setShowAll] = useState(false);
  const [sheetNote, setSheetNote] = useState('');
  const [landed, setLanded] = useState(null);

  const [sequences, setSequences] = useState([]);
  const [sequenceId, setSequenceId] = useState('');
  const [sendNow, setSendNow] = useState(false);

  const [busy, setBusy] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState(null);
  // True while the CRM check is in flight; the buttons stay live, because an
  // import with no duplicate information is still a correct import.
  const [checking, setChecking] = useState(false);
  const [dupeScanTruncated, setDupeScanTruncated] = useState(false);
  const inputRef = useRef(null);
  // A second file dropped while the first check is in flight resolves in
  // whatever order the network gives them, and without this the slower answer
  // wins -- flagging rows in file B against the identities of file A.
  const startCheck = useRef(newestOnly()).current;

  // Restore a draft before anything else, so coming back from Sequences lands
  // on the same table you left.
  useEffect(() => {
    let draft = null;
    try {
      draft = readImportDraft(window.sessionStorage.getItem(IMPORT_STATE_KEY));
    } catch {
      draft = null;
    }
    if (!draft) return;
    setFileName(draft.fileName);
    setSheetNote(draft.sheetNote);
    const restored = load(draft.text, { keep: draft.chosen });
    // Matches are derived, not stored: re-deriving on restore is cheaper than
    // widening the draft shape, and it also picks up anything imported from
    // another tab while this page was away.
    checkKnown(restored.rows);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    setSheetNote('');

    let text = '';
    // Tracked locally as well as in state: the draft below is written in this
    // same tick, and state set here is not readable until the next render.
    let note = '';
    if (/\.xlsx?m?$/i.test(file.name)) {
      // A workbook is a zip of XML the browser cannot read, so it makes one
      // round trip to be converted. Nothing is stored by that route -- the
      // review-before-writing property is unchanged.
      setBusy('reading');
      try {
        const converted = await api.workbookToCsv(file);
        text = converted.delimited ?? '';
        if ((converted.sheets ?? []).length > 1) {
          note =
            `Read the sheet “${converted.sheet}”. This workbook also has: `
            + `${converted.sheets.filter((s) => s !== converted.sheet).join(', ')}.`;
        }
        if (converted.truncated) {
          note = `${note} Only the first rows were read — the sheet is very large.`;
        }
        setSheetNote(note);
      } catch (caught) {
        setImportRows([]);
        setColumns({});
        setError(caught);
        setBusy('');
        return;
      }
      setBusy('');
    } else {
      try {
        text = await file.text();
      } catch {
        setParseError('Could not read that file.');
        return;
      }
    }

    const { keys, rows } = load(text, { keep: null });
    remember({ text, fileName: file.name, sheetNote: note, chosen: keys });
    checkKnown(rows);
  }

  // Ask the server which rows are already contacts, then untick them. Nothing
  // here blocks the import: a failure leaves the table unflagged, the same
  // forgiveness the sequence picker takes, because "we could not check" must
  // not become "you cannot import".
  async function checkKnown(rows) {
    const people = knownLookupPayload(rows);
    if (!people.length) return;
    const isNewest = startCheck();
    setChecking(true);
    setDupeScanTruncated(false);
    try {
      const answer = await api.knownContacts({ people });
      if (!isNewest()) return;
      const flagged = applyKnownMatches(rows, answer?.matches);
      setImportRows(flagged);
      setDupeScanTruncated(Boolean(answer?.truncated));
      // Pruned from whatever is selected *now*, not from the selection this
      // check started with: the recruiter can tick and untick while it is in
      // flight, and replacing the set wholesale threw that work away.
      setChosen((previous) => {
        const next = new Set(withoutDuplicates([...previous], flagged));
        persistChosen(next);
        return next;
      });
    } catch {
      /* the table just stays unflagged; importing still works */
    } finally {
      if (isNewest()) setChecking(false);
    }
  }

  // One place that turns delimited text into the review table, used by both a
  // fresh file and a restored draft. Returns the keys it selected AND the rows
  // it built, because the caller has to write the keys to the draft and hand
  // the rows to the duplicate check before React has re-rendered.
  function load(text, { keep }) {
    const { headers, rows } = parseDelimited(text);
    if (!rows.length) {
      setImportRows([]);
      setColumns({});
      setParseError('No rows in that file — is it the right export?');
      return { keys: [], rows: [] };
    }
    const detected = detectColumns(headers);
    const built = buildImportRows(rows, detected);
    setColumns(detected);
    setImportRows(built);
    // Pre-select exactly the rows that can be mailed unless we are restoring a
    // selection. Everything else is visible but off, so the default action is
    // never the surprising one.
    const keys = keep ? pruneChosen(keep, built) : built.filter((r) => r.sendable).map((r) => r.key);
    setChosen(new Set(keys));
    setShowAll(false);
    return { keys, rows: built };
  }

  // sessionStorage, not state: routing to Sequences unmounts this page, and
  // losing the file on the way back reads as the screen wiping your work.
  // Wrapped because a private window or a full quota throws on write.
  //
  // Every field is passed in rather than read off state: the only caller runs
  // in the same tick as the setState calls that produced them, so the closure
  // still holds the PREVIOUS file's name and selection -- which is how a
  // 200-row upload came back with nothing ticked and no filename, and how a
  // second upload came back as file B's text under file A's name.
  function remember({ text, fileName: name, sheetNote: note, chosen: chosenKeys }) {
    try {
      window.sessionStorage.setItem(
        IMPORT_STATE_KEY,
        JSON.stringify({
          fileName: name,
          text,
          sheetNote: note,
          chosen: chosenKeys,
        }),
      );
    } catch {
      /* the draft just will not survive a navigation */
    }
  }

  function forget() {
    try {
      window.sessionStorage.removeItem(IMPORT_STATE_KEY);
    } catch {
      /* nothing to clean up */
    }
  }

  // Persisting the selection separately from the text: the file is written once,
  // the checkboxes change constantly.
  function persistChosen(next) {
    try {
      const raw = window.sessionStorage.getItem(IMPORT_STATE_KEY);
      const draft = readImportDraft(raw);
      if (!draft) return;
      window.sessionStorage.setItem(
        IMPORT_STATE_KEY,
        JSON.stringify({ ...draft, chosen: [...next] }),
      );
    } catch {
      /* selection just will not survive a navigation */
    }
  }

  function toggle(key) {
    setChosen((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      persistChosen(next);
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
      persistChosen(next);
      return next;
    });
  }

  function removeDuplicates() {
    setChosen((previous) => {
      const next = new Set(withoutDuplicates([...previous], importRows));
      persistChosen(next);
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
    setSheetNote('');
    setLanded(null);
    setError(null);
    setChecking(false);
    setDupeScanTruncated(false);
    forget();
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
        setLanded({ count: ids.length, sequenceId: '', sequenceName: '' });
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
      setLanded({ count: ids.length, sequenceId, sequenceName: name });
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="stack">
      <div className="card stack">
        <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <input
            ref={inputRef}
            type="file"
            accept=".csv,.tsv,.txt,.xlsx,.xlsm,text/csv"
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
          A sourcing export (.csv) or an Excel workbook (.xlsx), with the contact columns
          ticked — “Personal Email” and “Work Email”. An export without them imports
          profiles nobody can be mailed.
        </div>
        {busy === 'reading' ? <div className="small muted">Reading the workbook…</div> : null}
        {sheetNote ? <div className="small muted" data-testid="import-sheet">{sheetNote}</div> : null}
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
                Re-export with “Personal Email” ticked.
              </Empty>
            ) : null}
            {checking ? (
              <div className="small muted" data-testid="import-checking">
                Checking these against your contacts…
              </div>
            ) : null}
            {summary.duplicates ? (
              <div className="small muted" data-testid="import-duplicate-note">
                {summary.duplicates} row{summary.duplicates === 1 ? '' : 's'} already
                {summary.duplicates === 1 ? ' matches' : ' match'} someone in Contacts and
                {summary.duplicates === 1 ? ' has' : ' have'} been unticked. Importing one
                anyway is safe — it updates the record it matched rather than adding a second —
                but a match on name and company alone can be two different people, so check
                before you re-tick it.
              </div>
            ) : null}
            {dupeScanTruncated ? (
              <div className="small muted" data-testid="import-duplicate-truncated">
                This workspace has more contacts than the LinkedIn check scans, so a row with no
                badge is not proof it is new. Address matches are unaffected.
              </div>
            ) : null}
            {summary.undeliverableOnly ? (
              <div className="small muted">
                {summary.undeliverableOnly} row
                {summary.undeliverableOnly === 1 ? '' : 's'} had only an address the export marked
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
            {landed ? (
              <div className="small muted" data-testid="import-landed">
                Where it went:{' '}
                <Link to="/contacts">Contacts</Link> (imported people are tracked in the
                CRM){landed.sequenceId ? (
                  <>
                    {', and '}
                    <Link to={`/sequences/${landed.sequenceId}`}>{landed.sequenceName}</Link>
                    {' for the enrollments and what has been sent'}
                  </>
                ) : null}
                . Nothing is added to a project — enrolling is what starts the outreach.
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
              {summary.duplicates ? (
                <button
                  type="button"
                  className="tiny"
                  onClick={removeDuplicates}
                  data-testid="import-remove-duplicates"
                >
                  Remove {summary.duplicates} duplicate
                  {summary.duplicates === 1 ? '' : 's'} from the selection
                </button>
              ) : null}
            </div>

            <table className="jobs-table">
              <thead>
                <tr>
                  <th style={{ width: 32 }} aria-label="select" />
                  <th>Name</th>
                  <th>Title</th>
                  <th>Company</th>
                  <th>Email we would use</th>
                  <th>Already in contacts</th>
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
                    <td>
                      <DuplicateCell row={row} />
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
