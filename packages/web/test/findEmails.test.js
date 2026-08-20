// Folding a find-emails response back into the selection, and telling the user
// what actually happened.
//
// mergeFoundEmails decides which address ends up at emails[0], and emails[0] is
// what the sequencer sends to. Getting the order wrong here puts a recruiting
// pitch in somebody's work inbox.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  describeFindEmails,
  forayHitToCandidate,
  mergeFoundEmails,
  partitionByEmail,
} from '../src/lib/forayImport.js';

const bare = (login) => ({ login, name: login, emails: [] });

test('a found personal address lands on the hit', () => {
  const [hit] = mergeFoundEmails([bare('ada')], {
    ada: { status: 'found', personal: ['ada@gmail.com'] },
  });
  assert.deepEqual(hit.emails, [
    { email: 'ada@gmail.com', source: 'contactout_personal', is_work: false, verified: false },
  ]);
});

test('personal comes before work, because emails[0] is what gets sent to', () => {
  const [hit] = mergeFoundEmails([bare('ada')], {
    ada: { status: 'found', personal: ['ada@gmail.com'], work: ['ada@stripe.com'] },
  });
  assert.deepEqual(
    hit.emails.map((e) => e.email),
    ['ada@gmail.com', 'ada@stripe.com'],
  );
});

test('found addresses go ahead of ones already on the hit', () => {
  // A candidate whose only address was a work one: the new personal address has
  // to win, or the import still pitches them at the office.
  const existing = { email: 'ada@stripe.com', source: 'apollo_work', is_work: true };
  const [hit] = mergeFoundEmails([{ ...bare('ada'), emails: [existing] }], {
    ada: { status: 'found', personal: ['ada@gmail.com'] },
  });
  assert.equal(hit.emails[0].email, 'ada@gmail.com');
  assert.equal(hit.emails.length, 2);
});

test('an address we already had is not duplicated', () => {
  const existing = { email: 'ada@gmail.com', source: 'commit', is_work: false };
  const [hit] = mergeFoundEmails([{ ...bare('ada'), emails: [existing] }], {
    ada: { status: 'found', personal: ['ada@gmail.com'] },
  });
  assert.deepEqual(hit.emails, [existing]);
});

test('a hit with no outcome is returned untouched', () => {
  const hit = bare('ada');
  const [same] = mergeFoundEmails([hit], { someone_else: { status: 'found', personal: ['x@y.z'] } });
  assert.equal(same, hit);
});

test('a miss is returned untouched rather than given an empty address', () => {
  const hit = bare('ada');
  const [same] = mergeFoundEmails([hit], { ada: { status: 'none', personal: [], work: [] } });
  assert.equal(same, hit);
});

test('a merged hit imports with the personal address as primary', () => {
  // The whole point of the ordering: forayHitToCandidate flattens emails in
  // order and the first one becomes Candidate.primary_email.
  const [hit] = mergeFoundEmails([bare('ada')], {
    ada: { status: 'found', personal: ['ada@gmail.com'], work: ['ada@stripe.com'] },
  });
  assert.equal(forayHitToCandidate(hit).emails[0], 'ada@gmail.com');
});

test('a merged hit stops counting as email-less', () => {
  const [hit] = mergeFoundEmails([bare('ada')], {
    ada: { status: 'found', personal: ['ada@gmail.com'] },
  });
  assert.equal(partitionByEmail([hit]).withoutEmail.length, 0);
});

test('the summary distinguishes "nothing found" from "we already had one"', () => {
  const text = describeFindEmails(
    { found_personal: 2 },
    {
      a: { status: 'found' },
      b: { status: 'found' },
      c: { status: 'none' },
      d: { status: 'has_personal' },
      e: { status: 'cached' },
      f: { status: 'no_profile' },
      g: { status: 'unknown' },
    },
  );
  assert.match(text, /2 personal addresses found/);
  assert.match(text, /1 with nothing on file/);
  assert.match(text, /1 already had one/);
  assert.match(text, /1 looked up before/);
  assert.match(text, /1 with no LinkedIn profile to match/);
  assert.match(text, /1 not in the sourcing database/);
});

test('a single find reads as singular', () => {
  assert.match(describeFindEmails({ found_personal: 1 }, { a: { status: 'found' } }),
    /^1 personal address found\.$/);
});

test('failures are surfaced, not folded into "nothing found"', () => {
  // A vendor error is not stamped server-side, so the user can retry -- but only
  // if they are told there is something to retry.
  const text = describeFindEmails({ found_personal: 0 }, { a: { status: 'error' } });
  assert.match(text, /1 failed/);
});

test('the summary falls back to counting statuses when no summary is given', () => {
  const text = describeFindEmails(undefined, { a: { status: 'found' }, b: { status: 'found' } });
  assert.match(text, /2 personal addresses found/);
});
