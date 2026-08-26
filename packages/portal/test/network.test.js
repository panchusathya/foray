import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  atCompany,
  basisLabel,
  companiesOf,
  mapLimit,
  matchesQuery,
  mergePeople,
  sortPeople,
} from '../src/lib/network.js';

const application = (id, company, title = 'Engineer') => ({
  id,
  company,
  title,
  status: 'submitted',
});

test('one person at two applications becomes one row', () => {
  const people = mergePeople([
    {
      application: application('a1', 'Stripe'),
      suggestions: [{ candidate_id: 'c1', name: 'Ada Lovelace', basis: 'former' }],
    },
    {
      application: application('a2', 'Stripe', 'Staff Engineer'),
      suggestions: [{ candidate_id: 'c1', name: 'Ada Lovelace', basis: 'current' }],
    },
  ]);

  assert.equal(people.length, 1);
  assert.equal(people[0].at.length, 2);
  // "Works there" beats "used to" for the same person.
  assert.equal(people[0].basis, 'current');
});

test('merging fills in whatever the first sighting was missing', () => {
  const people = mergePeople([
    {
      application: application('a1', 'Stripe'),
      suggestions: [{ candidate_id: 'c1', name: 'Ada', title: '', linkedin_url: '' }],
    },
    {
      application: application('a2', 'Stripe'),
      suggestions: [
        {
          candidate_id: 'c1',
          name: 'Ada',
          title: 'Staff Engineer',
          linkedin_url: 'https://example.test/ada',
          draft: 'hi Ada',
        },
      ],
    },
  ]);

  assert.equal(people[0].title, 'Staff Engineer');
  assert.equal(people[0].linkedin_url, 'https://example.test/ada');
  assert.equal(people[0].draft, 'hi Ada');
  // The draft has to remember which application it was written against — that
  // is the id the re-draft endpoint takes.
  assert.equal(people[0].draftFor, 'a2');
});

test('contacts with no id, and entries with no application, are skipped', () => {
  const people = mergePeople([
    { application: application('a1', 'Stripe'), suggestions: [{ name: 'Nobody' }] },
    { application: null, suggestions: [{ candidate_id: 'c9', name: 'Ghost' }] },
    null,
  ]);
  assert.deepEqual(people, []);
});

test('people with a written message come first', () => {
  const sorted = sortPeople([
    { name: 'Zoe', basis: 'current', draft: '', at: [] },
    { name: 'Ada', basis: 'former', draft: 'hello', at: [] },
    { name: 'Bob', basis: 'current', draft: '', at: [] },
  ]);
  assert.deepEqual(
    sorted.map((person) => person.name),
    ['Ada', 'Bob', 'Zoe'],
  );
});

test('company filters are counted and ordered by size', () => {
  const people = [
    { at: [{ company: 'Stripe' }, { company: 'Stripe' }] },
    { at: [{ company: 'Anthropic' }] },
    { at: [{ company: 'Stripe' }] },
  ];
  assert.deepEqual(companiesOf(people), [
    { company: 'Stripe', count: 3 },
    { company: 'Anthropic', count: 1 },
  ]);
});

test('filtering by company matches any of a person"s applications', () => {
  const person = { name: 'Ada', at: [{ company: 'Stripe' }, { company: 'Anthropic' }] };
  assert.equal(atCompany(person, 'Anthropic'), true);
  assert.equal(atCompany(person, 'Databricks'), false);
  assert.equal(atCompany(person, ''), true);
});

test('search covers the person and where they matter', () => {
  const person = {
    name: 'Ada Lovelace',
    title: 'Staff Engineer',
    company: 'Stripe',
    at: [{ company: 'Stripe', title: 'Backend Engineer' }],
  };
  assert.equal(matchesQuery(person, 'lovelace'), true);
  assert.equal(matchesQuery(person, 'backend'), true);
  assert.equal(matchesQuery(person, ''), true);
  assert.equal(matchesQuery(person, 'databricks'), false);
});

test('basis reads as English, and an unknown basis passes through', () => {
  assert.equal(basisLabel('current'), 'Works there');
  assert.equal(basisLabel('former'), 'Used to work there');
  assert.equal(basisLabel(''), '');
});

test('mapLimit keeps order and never exceeds the limit', async () => {
  let running = 0;
  let peak = 0;
  const results = await mapLimit([1, 2, 3, 4, 5, 6, 7], 3, async (value) => {
    running += 1;
    peak = Math.max(peak, running);
    await new Promise((resolve) => setTimeout(resolve, 5));
    running -= 1;
    return value * 2;
  });
  assert.deepEqual(results, [2, 4, 6, 8, 10, 12, 14]);
  assert.ok(peak <= 3, `ran ${peak} at once`);
});

test('one failing lookup nulls its slot instead of failing the batch', async () => {
  const results = await mapLimit([1, 2, 3], 2, async (value) => {
    if (value === 2) throw new Error('nope');
    return value;
  });
  assert.deepEqual(results, [1, null, 3]);
});

test('mapLimit copes with an empty list', async () => {
  assert.deepEqual(await mapLimit([], 4, async (value) => value), []);
});
