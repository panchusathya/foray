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
} from '../src/lib/forayImport.js';

const bare = (login) => ({ login, name: login, emails: [] });

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

test('a merged hit imports with the personal address as primary', () => {
  // The whole point of the ordering: forayHitToCandidate flattens emails in
  // order and the first one becomes Candidate.primary_email.
  const [hit] = mergeFoundEmails([bare('ada')], {
    ada: { status: 'found', personal: ['ada@gmail.com'], work: ['ada@stripe.com'] },
  });
  assert.equal(forayHitToCandidate(hit).emails[0], 'ada@gmail.com');
});

test('failures are surfaced, not folded into "nothing found"', () => {
  // A vendor error is not stamped server-side, so the user can retry -- but only
  // if they are told there is something to retry.
  const text = describeFindEmails({ found_personal: 0 }, { a: { status: 'error' } });
  assert.match(text, /1 failed/);
});
