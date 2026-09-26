import assert from 'node:assert/strict';
import { test } from 'node:test';

import { parseCsv } from '../src/connectors/file.js';
import { dedupe, toCandidate } from '../src/normalize.js';

test('drops records with no usable identity', () => {
  assert.equal(toCandidate({ company: 'Acme' }), null);
  assert.equal(toCandidate(null), null);
});

test('drops invalid email addresses', () => {
  const candidate = toCandidate({ name: 'X Y', emails: ['nope', 'ok@example.com'] });
  assert.deepEqual(candidate.emails, ['ok@example.com']);
});

test('dedupe keeps the last record per source key', () => {
  const deduped = dedupe([
    { source: 'a', source_id: '1', full_name: 'First' },
    { source: 'a', source_id: '1', full_name: 'Second' },
    { source: 'a', source_id: '2', full_name: 'Other' },
  ]);
  assert.equal(deduped.length, 2);
  assert.equal(deduped[0].full_name, 'Second');
});

test('csv parser handles quotes, commas and newlines', () => {
  const rows = parseCsv('name,note\n"Ada, A","said ""hi""\nagain"\n');
  assert.deepEqual(rows[1], ['Ada, A', 'said "hi"\nagain']);
});
