import assert from 'node:assert/strict';
import { test } from 'node:test';

import { csvToRecords, parseCsv } from '../src/connectors/file.js';
import { _internal } from '../src/connectors/github.js';
import { dedupe, toCandidate } from '../src/normalize.js';

test('maps a rich record onto the candidate contract', () => {
  const candidate = toCandidate(
    {
      id: 'abc',
      name: 'Ada Okonkwo',
      headline: 'Senior ML Engineer',
      location: 'San Francisco, CA',
      email: 'ada@example.com',
      skills: 'python, pytorch; llm',
      experiences: [
        { company: 'Modal', title: 'Senior ML Engineer', current: true, start_date: '2021-01' },
      ],
      education: [{ school: 'Stanford', degree: 'MS', graduation_year: '2016' }],
    },
    { source: 'ats' },
  );

  assert.equal(candidate.source, 'ats');
  assert.equal(candidate.source_id, 'abc');
  assert.equal(candidate.full_name, 'Ada Okonkwo');
  assert.deepEqual(candidate.emails, ['ada@example.com']);
  assert.deepEqual(candidate.skills, ['python', 'pytorch', 'llm']);
  assert.equal(candidate.experiences[0].is_current, true);
  assert.equal(candidate.educations[0].end_year, 2016);
});

test('builds a current role from a flat CSV row', () => {
  const candidate = toCandidate(
    { 'Full Name': 'Bo Lindqvist', Company: 'Linear', title: 'Frontend Engineer' },
    { source: 'csv' },
  );
  assert.equal(candidate.experiences.length, 1);
  assert.equal(candidate.experiences[0].company, 'Linear');
  assert.equal(candidate.experiences[0].is_current, true);
});

test('drops records with no usable identity', () => {
  assert.equal(toCandidate({ company: 'Acme' }), null);
  assert.equal(toCandidate(null), null);
});

test('drops invalid email addresses', () => {
  const candidate = toCandidate({ name: 'X Y', emails: ['nope', 'ok@example.com'] });
  assert.deepEqual(candidate.emails, ['ok@example.com']);
});

test('assembles a name from first and last when full name is absent', () => {
  const candidate = toCandidate({ first_name: 'Cleo', last_name: 'Rahman' });
  assert.equal(candidate.full_name, 'Cleo Rahman');
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

test('csvToRecords maps headers to values', () => {
  const records = csvToRecords('Full Name,Company\nAda,Modal\n');
  assert.deepEqual(records, [{ 'Full Name': 'Ada', Company: 'Modal' }]);
});

test('github profile becomes a candidate with ranked languages', () => {
  const candidate = _internal.toCandidateFromProfile(
    { id: 7, login: 'ada', name: 'Ada O', bio: 'ML infra', company: '@Modal', location: 'SF' },
    [
      { name: 'infer', language: 'Python', stargazers_count: 40, fork: false },
      { name: 'tools', language: 'Rust', stargazers_count: 2, fork: false },
      { name: 'copy', language: 'Java', stargazers_count: 900, fork: true },
    ],
  );
  assert.equal(candidate.source, 'github');
  assert.equal(candidate.full_name, 'Ada O');
  assert.deepEqual(candidate.skills, ['Python', 'Rust']);
  assert.equal(candidate.experiences[0].company, 'Modal');
  assert.match(candidate.summary, /infer/);
});
