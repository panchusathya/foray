import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  BULK_CHUNK,
  VERIFICATION_SCORES,
  buildImportRows,
  chunk,
  detectColumns,
  hasEmailColumn,
  juiceboxRowToCandidate,
  knownLookupPayload,
  linkedinSlug,
  parseDelimited,
  pruneChosen,
  readImportDraft,
  selectEmails,
  selectPhones,
  sourceIdFor,
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
});

describe('buildImportRows', () => {
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

  it('only builds a payload for a row it would actually send', () => {
    assert.ok(importRows[0].payload);
    for (const row of importRows.slice(1)) assert.equal(row.payload, null);
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

  it('drops a key from a different file rather than claiming a row', () => {
    assert.deepEqual(pruneChosen(['0:li:someone-else'], importRows), []);
    assert.deepEqual(pruneChosen(undefined, importRows), []);
  });

  it('never restores a row that cannot be mailed', () => {
    const blocked = importRows.find((r) => !r.sendable);
    assert.deepEqual(pruneChosen([blocked.key], importRows), []);
  });

  it('refuses junk instead of throwing on mount', () => {
    assert.equal(readImportDraft(null), null);
    assert.equal(readImportDraft(''), null);
    assert.equal(readImportDraft('not json'), null);
    assert.equal(readImportDraft('{"text":""}'), null);
    assert.equal(readImportDraft('{"nope":1}'), null);
  });
});

describe('selectPhones', () => {
  const columns = detectColumns(parseDelimited(fixture()).headers);

  it('splits a cell carrying several', () => {
    assert.deepEqual(
      selectPhones({ 'Phone Numbers': '+12134317465, (415) 555-0123; 646-555-9999' }, columns),
      ['+12134317465', '(415) 555-0123', '646-555-9999'],
    );
  });

  it('drops something too short to be a subscriber number', () => {
    assert.deepEqual(selectPhones({ 'Phone Numbers': 'x123, 4567' }, columns), []);
  });

  it('puts phones on the payload, so an inbound caller can be attributed', () => {
    const { columns: c, rows } = parsedFixture(
      '"Ada","Lovelace","SF","https://linkedin.com/in/ada","ada@gmail.com","Deliverable","","",'
      + '"+12134317465","mobile","valid","","Research Engineer","Meta","","","","",""',
    );
    assert.deepEqual(juiceboxRowToCandidate(rows[0], c).phones, ['+12134317465']);
  });
});

describe('the already-in-contacts check', () => {
  const { columns, rows } = parsedFixture(
    '"Ada","Lovelace","SF","https://linkedin.com/in/ada","ada@gmail.com","Deliverable",'
    + '"ada@meta.com","Risky","","","","","Research Engineer","Meta","","","","",""',
  );
  const importRows = buildImportRows(rows, columns);

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
  });
});
