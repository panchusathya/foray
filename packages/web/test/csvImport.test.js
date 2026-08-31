import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  BULK_CHUNK,
  CSV_SOURCE,
  VERIFICATION_SCORES,
  applyKnownMatches,
  buildImportRows,
  chunk,
  describeImport,
  detectColumns,
  hasEmailColumn,
  juiceboxRowToCandidate,
  knownLookupPayload,
  linkedinSlug,
  normalizeEmail,
  parseDelimited,
  pruneChosen,
  readImportDraft,
  rowName,
  selectEmails,
  selectPhones,
  sourceIdFor,
  summarizeImport,
  withoutDuplicates,
} from '../src/lib/csvImport.js';

// The real export header, verbatim from a Juicebox download. Kept exact so a
// column rename upstream fails a test rather than silently importing nulls.
const HEADER = '"First name","Last name","Location","LinkedIn","Personal Email",'
  + '"Personal Email Verification","Work Email","Work Email Verification","Phone Numbers",'
  + '"Phone Line Type","Phone Verification Status","GitHub","Current Title",'
  + '"Current Org Name","Education","Average Tenure (Years)","Current Tenure (Years)",'
  + '"Total Experience (Years)","Juicebox Profile URL"';

function fixture(...rows) {
  return [HEADER, ...rows].join('\n');
}

function parsedFixture(...rows) {
  const { headers, rows: parsed } = parseDelimited(fixture(...rows));
  return { columns: detectColumns(headers), rows: parsed };
}

describe('parseDelimited', () => {
  it('keeps commas that live inside a quoted field', () => {
    const { rows } = parsedFixture(
      '"Ada","Lovelace","San Francisco Bay Area, California, United States",'
      + '"https://linkedin.com/in/ada","ada@example.com","Deliverable","","","","","",'
      + '"","Research Engineer","Meta","Oxford, MA","2","1","9","https://app.juicebox.ai/p/1"',
    );
    assert.equal(rows.length, 1);
    assert.equal(rows[0].Location, 'San Francisco Bay Area, California, United States');
    assert.equal(rows[0]['Current Org Name'], 'Meta');
    assert.equal(rows[0].Education, 'Oxford, MA');
  });

  it('handles CRLF, a BOM, and a doubled quote', () => {
    const text = '﻿"a","b"\r\n"say ""hi""","2"\r\n';
    const { headers, rows } = parseDelimited(text);
    assert.deepEqual(headers, ['a', 'b']);
    assert.deepEqual(rows, [{ a: 'say "hi"', b: '2' }]);
  });

  it('skips blank lines instead of emitting empty rows', () => {
    const { rows } = parseDelimited('"a","b"\n"1","2"\n\n\n"3","4"\n');
    assert.equal(rows.length, 2);
  });

  it('reads a tab-separated file too', () => {
    const { headers, rows } = parseDelimited('a\tb\n1\t2\n');
    assert.deepEqual(headers, ['a', 'b']);
    assert.deepEqual(rows, [{ a: '1', b: '2' }]);
  });

  it('returns nothing for an empty file rather than throwing', () => {
    assert.deepEqual(parseDelimited(''), { headers: [], rows: [] });
    assert.deepEqual(parseDelimited('   \n'), { headers: [], rows: [] });
  });
});

describe('detectColumns', () => {
  it('finds every column the real export carries', () => {
    const { headers } = parseDelimited(fixture());
    const columns = detectColumns(headers);
    assert.equal(columns.firstName, 'First name');
    assert.equal(columns.personalEmail, 'Personal Email');
    assert.equal(columns.personalEmailStatus, 'Personal Email Verification');
    assert.equal(columns.workEmail, 'Work Email');
    assert.equal(columns.linkedin, 'LinkedIn');
    assert.equal(columns.currentCompany, 'Current Org Name');
    assert.equal(columns.totalExperience, 'Total Experience (Years)');
    assert.ok(hasEmailColumn(columns));
  });

  it('is insensitive to case and spacing', () => {
    const columns = detectColumns(['FIRST  NAME', 'personal email']);
    assert.equal(columns.firstName, 'FIRST  NAME');
    assert.equal(columns.personalEmail, 'personal email');
  });

  it('reports an export that carries no address at all', () => {
    // The other real export shape: profile data, no contact columns.
    const columns = detectColumns(['First name', 'Last name', 'Tags', 'Location', 'LinkedIn']);
    assert.equal(hasEmailColumn(columns), false);
  });
});

describe('selectEmails', () => {
  const columns = detectColumns(parseDelimited(fixture()).headers);

  it('puts the personal address first, because emails[0] is what gets mailed', () => {
    const row = {
      'Personal Email': 'ada@gmail.com',
      'Personal Email Verification': 'Deliverable',
      'Work Email': 'ada@meta.com',
      'Work Email Verification': 'Deliverable',
    };
    const { usable } = selectEmails(row, columns);
    assert.deepEqual(usable.map((e) => e.email), ['ada@gmail.com', 'ada@meta.com']);
    assert.equal(usable[0].kind, 'personal');
  });

  it('drops an undeliverable address instead of trusting the reach floor', () => {
    const row = {
      'Personal Email': 'bounces@example.com',
      'Personal Email Verification': 'Undeliverable',
      'Work Email': 'ada@meta.com',
      'Work Email Verification': 'Deliverable',
    };
    const { usable, excluded } = selectEmails(row, columns);
    assert.deepEqual(usable.map((e) => e.email), ['ada@meta.com']);
    assert.deepEqual(excluded.map((e) => e.email), ['bounces@example.com']);
  });

  it('keeps a risky address but scores it low', () => {
    const row = { 'Work Email': 'ada@meta.com', 'Work Email Verification': 'Risky' };
    const { usable } = selectEmails(row, columns);
    assert.equal(usable[0].verification, 'risky');
    assert.ok(VERIFICATION_SCORES.risky < VERIFICATION_SCORES.deliverable);
  });

  it('de-duplicates the same address appearing in both columns', () => {
    const row = {
      'Personal Email': 'Ada@Example.com',
      'Work Email': 'ada@example.com',
    };
    const { usable } = selectEmails(row, columns);
    assert.deepEqual(usable.map((e) => e.email), ['ada@example.com']);
  });

  it('rejects a cell that is not an address', () => {
    assert.equal(normalizeEmail('not an email'), '');
    assert.equal(normalizeEmail('ada@'), '');
    assert.equal(normalizeEmail(''), '');
    assert.equal(normalizeEmail('  Ada@Example.COM '), 'ada@example.com');
  });
});

describe('sourceIdFor', () => {
  const columns = detectColumns(parseDelimited(fixture()).headers);

  it('prefers the LinkedIn slug so a re-upload updates rather than duplicates', () => {
    const a = sourceIdFor({ LinkedIn: 'https://www.linkedin.com/in/ada-lovelace/' }, columns);
    const b = sourceIdFor({ LinkedIn: 'linkedin.com/in/ada-lovelace' }, columns);
    assert.equal(a, 'li:ada-lovelace');
    assert.equal(a, b);
  });

  it('falls back through profile url, email, then name', () => {
    assert.equal(
      sourceIdFor({ 'Juicebox Profile URL': 'https://app.juicebox.ai/p/7' }, columns),
      'jb:https://app.juicebox.ai/p/7',
    );
    assert.equal(sourceIdFor({ 'Personal Email': 'Ada@x.com' }, columns), 'email:ada@x.com');
    assert.equal(
      sourceIdFor({ 'First name': 'Ada', 'Last name': 'Lovelace', 'Current Org Name': 'Meta' }, columns),
      'name:ada-lovelace@meta',
    );
  });

  it('is empty when there is nothing at all to key on', () => {
    assert.equal(sourceIdFor({}, columns), '');
  });

  it('ignores a linkedin url with no /in/ segment', () => {
    assert.equal(linkedinSlug('https://linkedin.com/company/meta'), '');
  });
});

describe('juiceboxRowToCandidate', () => {
  const { columns, rows } = parsedFixture(
    '"Ada","Lovelace","San Francisco Bay Area, California, United States",'
    + '"https://linkedin.com/in/ada","ada@gmail.com","Deliverable","ada@meta.com","Risky","","","",'
    + '"https://github.com/ada","Research Engineer","Meta","Oxford","2","1","9.5",'
    + '"https://app.juicebox.ai/p/1"',
  );
  const payload = juiceboxRowToCandidate(rows[0], columns);

  it('maps onto the CandidateIn shape the bulk endpoint accepts', () => {
    assert.equal(payload.source, CSV_SOURCE);
    assert.equal(payload.source_id, 'li:ada');
    assert.equal(payload.full_name, 'Ada Lovelace');
    assert.equal(payload.current_title, 'Research Engineer');
    assert.equal(payload.current_company, 'Meta');
    assert.equal(payload.github_url, 'https://github.com/ada');
    assert.equal(payload.location, 'San Francisco Bay Area, California, United States');
    assert.deepEqual(payload.emails, ['ada@gmail.com', 'ada@meta.com']);
  });

  it('scores each address the way the sequencer already reads scores', () => {
    assert.deepEqual(payload.raw.juicebox_csv.email_reach, {
      'ada@gmail.com': VERIFICATION_SCORES.deliverable,
      'ada@meta.com': VERIFICATION_SCORES.risky,
    });
  });

  it('omits years_experience when the column is blank, rather than sending 0', () => {
    assert.equal(payload.years_experience, 9.5);
    const { columns: c2, rows: r2 } = parsedFixture(
      '"Bo","Diddley","","https://linkedin.com/in/bo","bo@x.com","Deliverable","","","","","",'
      + '"","Engineer","Acme","","","","",""',
    );
    assert.ok(!('years_experience' in juiceboxRowToCandidate(r2[0], c2)));
  });

  it('records what it dropped, so a skipped address is explainable later', () => {
    const { columns: c3, rows: r3 } = parsedFixture(
      '"Cy","Young","","https://linkedin.com/in/cy","dead@x.com","Undeliverable",'
      + '"cy@acme.com","Deliverable","","","","","Engineer","Acme","","","","",""',
    );
    const p = juiceboxRowToCandidate(r3[0], c3);
    assert.deepEqual(p.emails, ['cy@acme.com']);
    assert.deepEqual(p.raw.juicebox_csv.excluded_emails, [
      { email: 'dead@x.com', reason: 'undeliverable' },
    ]);
  });
});

describe('buildImportRows and summarizeImport', () => {
  const { columns, rows } = parsedFixture(
    // sendable
    '"Ada","Lovelace","SF","https://linkedin.com/in/ada","ada@gmail.com","Deliverable","","",'
    + '"","","","","Research Engineer","Meta","","","","",""',
    // no email at all -- the common case in a real export
    '"Bo","Diddley","NY","https://linkedin.com/in/bo","","","","","","","","",'
    + '"Engineer","Acme","","","","",""',
    // only an undeliverable address
    '"Cy","Young","LA","https://linkedin.com/in/cy","dead@x.com","Undeliverable","","",'
    + '"","","","","Engineer","Acme","","","","",""',
    // no name
    '"","","LA","https://linkedin.com/in/dd","dd@x.com","Deliverable","","","","","","",'
    + '"Engineer","Acme","","","","",""',
  );
  const importRows = buildImportRows(rows, columns);

  it('marks each row with why it can or cannot be mailed', () => {
    assert.deepEqual(importRows.map((r) => r.blocked), [
      '',
      'no email',
      'only an undeliverable address',
      'no name',
    ]);
    assert.equal(importRows[0].primaryEmail, 'ada@gmail.com');
    assert.equal(importRows[0].verification, 'deliverable');
  });

  it('only builds a payload for a row it would actually send', () => {
    assert.ok(importRows[0].payload);
    for (const row of importRows.slice(1)) assert.equal(row.payload, null);
  });

  it('counts each exclusion reason separately', () => {
    const summary = summarizeImport(importRows);
    assert.deepEqual(summary, {
      total: 4,
      sendable: 1,
      undeliverableOnly: 1,
      noEmail: 1,
      unnamed: 1,
      unidentified: 0,
      duplicates: 0,
    });
  });

  it('says which, rather than only how many', () => {
    const text = describeImport(summarizeImport(importRows));
    assert.match(text, /4 rows/);
    assert.match(text, /1 with a usable email/);
    assert.match(text, /1 with no email/);
    assert.match(text, /undeliverable/);
    assert.equal(describeImport(summarizeImport([])), 'No rows in that file.');
  });

  it('gives every row a distinct key even when two rows are identical', () => {
    const dupes = buildImportRows([...rows, rows[0]], columns);
    assert.equal(new Set(dupes.map((r) => r.key)).size, dupes.length);
  });
});

describe('chunk', () => {
  it('splits to stay under the bulk endpoint cap', () => {
    const items = Array.from({ length: BULK_CHUNK + 3 }, (_, i) => i);
    const parts = chunk(items);
    assert.equal(parts.length, 2);
    assert.equal(parts[0].length, BULK_CHUNK);
    assert.equal(parts[1].length, 3);
    assert.deepEqual(chunk([]), []);
  });
});

describe('surviving a navigation', () => {
  const { columns, rows } = parsedFixture(
    '"Ada","Lovelace","SF","https://linkedin.com/in/ada","ada@gmail.com","Deliverable","","",'
    + '"","","","","Research Engineer","Meta","","","","",""',
    '"Bo","Diddley","NY","https://linkedin.com/in/bo","","","","","","","","",'
    + '"Engineer","Acme","","","","",""',
  );
  const importRows = buildImportRows(rows, columns);

  it('keeps a selection that still matches the rows', () => {
    const keys = importRows.filter((r) => r.sendable).map((r) => r.key);
    assert.deepEqual(pruneChosen(keys, importRows), keys);
  });

  it('drops a key from a different file rather than claiming a row', () => {
    assert.deepEqual(pruneChosen(['0:li:someone-else'], importRows), []);
    assert.deepEqual(pruneChosen(undefined, importRows), []);
  });

  it('never restores a row that cannot be mailed', () => {
    const blocked = importRows.find((r) => !r.sendable);
    assert.deepEqual(pruneChosen([blocked.key], importRows), []);
  });

  it('reads back a draft it wrote', () => {
    const draft = readImportDraft(
      JSON.stringify({ fileName: 'x.csv', text: 'a,b\n1,2\n', chosen: ['0:a'], sheetNote: 'n' }),
    );
    assert.equal(draft.fileName, 'x.csv');
    assert.equal(draft.chosen.length, 1);
    assert.equal(draft.sheetNote, 'n');
  });

  it('refuses junk instead of throwing on mount', () => {
    assert.equal(readImportDraft(null), null);
    assert.equal(readImportDraft(''), null);
    assert.equal(readImportDraft('not json'), null);
    assert.equal(readImportDraft('{"text":""}'), null);
    assert.equal(readImportDraft('{"nope":1}'), null);
  });

  it('tolerates a draft whose fields are the wrong type', () => {
    const draft = readImportDraft(JSON.stringify({ text: 'a\n1\n', fileName: 7, chosen: 'no' }));
    assert.equal(draft.fileName, '');
    assert.deepEqual(draft.chosen, []);
  });
});

describe('selectPhones', () => {
  const columns = detectColumns(parseDelimited(fixture()).headers);

  it('reads a single number as written', () => {
    assert.deepEqual(selectPhones({ 'Phone Numbers': '+1 213-431-7465' }, columns), ['+1 213-431-7465']);
  });

  it('splits a cell carrying several', () => {
    assert.deepEqual(
      selectPhones({ 'Phone Numbers': '+12134317465, (415) 555-0123; 646-555-9999' }, columns),
      ['+12134317465', '(415) 555-0123', '646-555-9999'],
    );
  });

  it('drops something too short to be a subscriber number', () => {
    assert.deepEqual(selectPhones({ 'Phone Numbers': 'x123, 4567' }, columns), []);
  });

  it('de-duplicates and tolerates an empty cell', () => {
    assert.deepEqual(selectPhones({ 'Phone Numbers': '2134317465, 2134317465' }, columns), ['2134317465']);
    assert.deepEqual(selectPhones({}, columns), []);
    assert.deepEqual(selectPhones({ 'Phone Numbers': '   ' }, columns), []);
  });

  it('puts phones on the payload, so an inbound caller can be attributed', () => {
    const { columns: c, rows } = parsedFixture(
      '"Ada","Lovelace","SF","https://linkedin.com/in/ada","ada@gmail.com","Deliverable","","",'
      + '"+12134317465","mobile","valid","","Research Engineer","Meta","","","","",""',
    );
    const payload = juiceboxRowToCandidate(rows[0], c);
    assert.deepEqual(payload.phones, ['+12134317465']);
    assert.equal(payload.raw.juicebox_csv.phone_line_type, 'mobile');
    assert.equal(payload.raw.juicebox_csv.phone_verification, 'valid');
  });

  it('sends an empty list when the export carries no phone column', () => {
    const { columns: c, rows } = parsedFixture(
      '"Bo","Diddley","NY","https://linkedin.com/in/bo","bo@x.com","Deliverable","","","","","",'
      + '"","Engineer","Acme","","","","",""',
    );
    assert.deepEqual(juiceboxRowToCandidate(rows[0], c).phones, []);
  });
});

describe('the already-in-contacts check', () => {
  const { columns, rows } = parsedFixture(
    // sendable, with a LinkedIn and a company
    '"Ada","Lovelace","SF","https://linkedin.com/in/ada","ada@gmail.com","Deliverable",'
    + '"ada@meta.com","Risky","","","","","Research Engineer","Meta","","","","",""',
    // sendable, second person
    '"Bo","Diddley","NY","https://linkedin.com/in/bo","bo@x.com","Deliverable","","",'
    + '"","","","","Engineer","Acme","","","","",""',
    // not sendable: only an undeliverable address
    '"Cy","Young","LA","https://linkedin.com/in/cy","dead@x.com","Undeliverable","","",'
    + '"","","","","Engineer","Acme","","","","",""',
  );
  const importRows = buildImportRows(rows, columns);

  it('asks about the rows it would actually import, and no others', () => {
    const people = knownLookupPayload(importRows);
    assert.deepEqual(
      people.map((p) => p.full_name),
      ['Ada Lovelace', 'Bo Diddley'],
    );
    assert.equal(people[0].key, importRows[0].key);
  });

  it('sends every address on the row, not just the one it would mail', () => {
    // An address the export marked risky or undeliverable is a bad one to mail
    // and a perfectly good one to recognise somebody by.
    const [ada] = knownLookupPayload(importRows);
    assert.deepEqual(ada.emails, ['ada@gmail.com', 'ada@meta.com']);
  });

  it('sends the raw LinkedIn URL, leaving the slug to the server', () => {
    // enrich.linkedin_url.linkedin_slug rstrips "._-" and linkedinSlug here
    // does not, so folding on the client would make the two disagree.
    const [ada] = knownLookupPayload(importRows);
    assert.equal(ada.linkedin_url, 'https://linkedin.com/in/ada');
    assert.equal(ada.company, 'Meta');
  });

  it('skips a row with nothing to match on', () => {
    const { columns: c, rows: r } = parsedFixture(
      '"Ada","Lovelace","SF","","ada@gmail.com","Deliverable","","","","","","",'
      + '"Research Engineer","","","","","",""',
    );
    const built = buildImportRows(r, c);
    // Has an address, so it is still worth asking about.
    assert.equal(knownLookupPayload(built).length, 1);
    // Strip the address and there is a name but no company, so nothing to ask.
    const nameOnly = built.map((row) => ({
      ...row,
      payload: { ...row.payload, emails: [], linkedin_url: '' },
      excludedEmails: [],
    }));
    assert.deepEqual(knownLookupPayload(nameOnly), []);
  });

  it('attaches a match to the row it names and leaves the rest alone', () => {
    const flagged = applyKnownMatches(importRows, [
      {
        key: importRows[0].key,
        candidate_id: 'cand_1',
        full_name: 'Ada Lovelace',
        matched_on: 'email',
        reason: 'same email: ada@gmail.com',
      },
    ]);
    assert.deepEqual(flagged[0].duplicateOf, {
      candidateId: 'cand_1',
      fullName: 'Ada Lovelace',
      matchedOn: 'email',
      reason: 'same email: ada@gmail.com',
    });
    assert.equal(flagged[1].duplicateOf, undefined);
    // Non-mutating: the table renders off these rows, and a stale answer
    // arriving after a second file has to be droppable.
    assert.equal(importRows[0].duplicateOf, undefined);
  });

  it('clears a flag the newest answer does not repeat', () => {
    const once = applyKnownMatches(importRows, [
      { key: importRows[0].key, candidate_id: 'c', full_name: 'A', matched_on: 'email', reason: 'r' },
    ]);
    const again = applyKnownMatches(once, []);
    assert.equal(again[0].duplicateOf, null);
  });

  it('tolerates a missing or empty match list', () => {
    assert.equal(applyKnownMatches(importRows, undefined)[0].duplicateOf, undefined);
    assert.equal(applyKnownMatches(importRows, null).length, 3);
  });

  it('counts duplicates among the sendable rows only', () => {
    const flagged = applyKnownMatches(importRows, [
      { key: importRows[0].key, candidate_id: 'c', full_name: 'A', matched_on: 'email', reason: 'r' },
      // Cy cannot be mailed, so this must not be counted twice -- once as
      // blocked and once as a duplicate.
      { key: importRows[2].key, candidate_id: 'd', full_name: 'C', matched_on: 'email', reason: 'r' },
    ]);
    assert.equal(summarizeImport(flagged).duplicates, 1);
  });

  it('says so in the summary line', () => {
    const flagged = applyKnownMatches(importRows, [
      { key: importRows[0].key, candidate_id: 'c', full_name: 'A', matched_on: 'email', reason: 'r' },
    ]);
    assert.match(describeImport(summarizeImport(flagged)), /1 already in contacts/);
    assert.doesNotMatch(describeImport(summarizeImport(importRows)), /already in contacts/);
  });

  it('drops duplicates from a selection and keeps everything else', () => {
    const flagged = applyKnownMatches(importRows, [
      { key: importRows[0].key, candidate_id: 'c', full_name: 'A', matched_on: 'email', reason: 'r' },
    ]);
    const chosen = [importRows[0].key, importRows[1].key];
    assert.deepEqual(withoutDuplicates(chosen, flagged), [importRows[1].key]);
    // Nothing flagged, nothing removed.
    assert.deepEqual(withoutDuplicates(chosen, importRows), chosen);
    assert.deepEqual(withoutDuplicates(null, flagged), []);
  });
});
