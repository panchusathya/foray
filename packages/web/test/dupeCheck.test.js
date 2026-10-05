import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { parseDelimited, parseTable } from '../src/lib/csvImport.js';
import {
  AUTO,
  NONE,
  SPLIT_NAME,
  applyMatches,
  buildCheckRows,
  chunkPeople,
  defaultMapping,
  guessColumns,
  identityFor,
  lookupPeople,
  outputName,
  summarize,
  writeTable,
} from '../src/lib/dupeCheck.js';

function load(text) {
  const table = parseTable(text);
  const guess = guessColumns(table.header);
  const mapping = defaultMapping(guess);
  return { table, guess, mapping, rows: buildCheckRows(table.cells, mapping, guess) };
}

describe('parseTable', () => {
  it('keeps repeated headers and untrimmed cells that parseDelimited folds away', () => {
    const table = parseTable('Name,Note,Note\r\n"Ada, L", first , second\r\n');
    assert.deepEqual(table.header, ['Name', 'Note', 'Note']);
    assert.deepEqual(table.cells, [['Ada, L', ' first ', ' second']]);
    assert.equal(table.delimiter, ',');
    assert.equal(table.bom, false);
  });

  it('reports a BOM and a tab delimiter', () => {
    const table = parseTable('﻿Name\tCompany\nAda\tAnalytical\n');
    assert.equal(table.bom, true);
    assert.equal(table.delimiter, '\t');
    assert.deepEqual(table.header, ['Name', 'Company']);
  });

  it('leaves parseDelimited behaving as before', () => {
    const { headers, rows } = parseDelimited(' Name , Email \n Ada , ada@x.com \n');
    assert.deepEqual(headers, ['Name', 'Email']);
    assert.deepEqual(rows, [{ Name: 'Ada', Email: 'ada@x.com' }]);
  });
});

describe('guessColumns', () => {
  it('finds the usual columns whatever they are called', () => {
    const guess = guessColumns([
      'Full Name',
      'LinkedIn Profile URL',
      'Work Email',
      'Work Email Status',
      'Personal_Email',
      'Company Name',
    ]);
    assert.equal(guess.full, 0);
    assert.deepEqual(guess.linkedin, [1]);
    // The status column is about an address, not one.
    assert.deepEqual(guess.emails, [2, 4]);
    assert.equal(guess.company, 5);
  });

  it('builds a name from first and last when there is no full-name column', () => {
    const guess = guessColumns(['First Name', 'Last Name', 'Organization']);
    assert.equal(defaultMapping(guess).name, SPLIT_NAME);
    assert.equal(guess.company, 2);
  });

  it('does not take "Last contacted" for a surname', () => {
    assert.equal(guessColumns(['Name', 'Last contacted']).last, -1);
  });
});

describe('identityFor', () => {
  it('reads every address in every email column', () => {
    const { rows } = load('Name,Email,Other Email\nAda,ADA@x.com; ada@y.com,ada@z.com\n');
    assert.deepEqual(rows[0].identity.emails, ['ada@x.com', 'ada@y.com', 'ada@z.com']);
  });

  it('only takes a whole-cell address or profile when no column is named for one', () => {
    const { rows } = load(
      'Who,Profile,Notes\n'
        + 'Ada,https://www.linkedin.com/in/ada-l/,referred by bob@acme.com\n'
        + 'Bob,bob@acme.com,\n',
    );
    assert.equal(rows[0].identity.linkedin_url, 'https://www.linkedin.com/in/ada-l/');
    // The note mentions Bob's address; that must not make this row Bob.
    assert.deepEqual(rows[0].identity.emails, []);
    assert.deepEqual(rows[1].identity.emails, ['bob@acme.com']);
  });

  it('ignores a company page in a LinkedIn column', () => {
    const { rows } = load('Name,Company LinkedIn\nAda,https://linkedin.com/company/acme\n');
    assert.equal(rows[0].identity.linkedin_url, '');
  });

  it('honours an override, including turning a role off', () => {
    const { table, guess, mapping } = load('A,B\nAda Lovelace,Analytical\n');
    const identity = identityFor(
      table.cells[0],
      { ...mapping, name: '0', company: '1', email: NONE, linkedin: NONE },
      guess,
    );
    assert.equal(identity.full_name, 'Ada Lovelace');
    assert.equal(identity.company, 'Analytical');
  });
});

describe('checkable rows', () => {
  it('never sends a bare name, which the server would not match anyway', () => {
    const { rows } = load('Name,Company\nAda Lovelace,\nGrace Hopper,Navy\n');
    assert.deepEqual(
      rows.map((r) => r.checkable),
      [false, true],
    );
    assert.deepEqual(lookupPeople(rows), [
      { key: '1', emails: [], linkedin_url: '', full_name: 'Grace Hopper', company: 'Navy' },
    ]);
  });

  it('chunks to the server ceiling', () => {
    const people = Array.from({ length: 12001 }, (_, i) => ({ key: String(i) }));
    assert.deepEqual(
      chunkPeople(people).map((c) => c.length),
      [5000, 5000, 2001],
    );
  });
});

describe('writing the file back', () => {
  const text = '﻿Name,Notes,LinkedIn\r\nAda,"likes, commas",https://linkedin.com/in/ada\r\n'
    + 'Bob, padded ,https://linkedin.com/in/bob\r\n';

  it('drops only the removed rows and keeps every cell as it came', () => {
    const { table, rows } = load(text);
    const flagged = applyMatches(rows, [
      { key: '0', candidate_id: 'cand_1', full_name: 'Ada', matched_on: 'linkedin', reason: 'x' },
    ]);
    const removed = new Set(flagged.filter((r) => r.match).map((r) => r.key));
    const out = writeTable({
      header: table.header,
      rows: flagged.filter((r) => !removed.has(r.key)).map((r) => r.cells),
      delimiter: table.delimiter,
      bom: table.bom,
    });
    assert.equal(out, '﻿Name,Notes,LinkedIn\r\nBob," padded ",https://linkedin.com/in/bob\r\n');
    // And it reads back to the same cells.
    assert.deepEqual(parseTable(out).cells, [['Bob', ' padded ', 'https://linkedin.com/in/bob']]);
  });

  it('round-trips quotes, commas and newlines', () => {
    const header = ['A', 'B'];
    const cells = [['say "hi"', 'one,\ntwo']];
    assert.deepEqual(parseTable(writeTable({ header, rows: cells })).cells, cells);
  });

  it('counts what goes in each file', () => {
    const { rows } = load(text);
    const flagged = applyMatches(rows, [
      { key: '1', candidate_id: 'c', full_name: 'Bob', matched_on: 'name_and_company', reason: '' },
    ]);
    const summary = summarize(flagged, new Set(['1']));
    assert.equal(summary.total, 2);
    assert.equal(summary.matched, 1);
    assert.equal(summary.weak, 1);
    assert.equal(summary.keeping, 1);
  });

  it('names the output after the input', () => {
    assert.equal(outputName('leads.xlsx', 'deduped'), 'leads-deduped.csv');
    assert.equal(outputName('leads.tsv', 'duplicates', '\t'), 'leads-duplicates.tsv');
    assert.equal(outputName('', 'deduped'), 'contacts-deduped.csv');
  });
});

it('AUTO is the default for both identifying columns', () => {
  const mapping = defaultMapping(guessColumns(['Name']));
  assert.equal(mapping.email, AUTO);
  assert.equal(mapping.linkedin, AUTO);
  assert.equal(mapping.company, NONE);
});
