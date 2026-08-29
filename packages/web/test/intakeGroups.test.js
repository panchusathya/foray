import assert from 'node:assert/strict';
import { test } from 'node:test';

import { counterpartyNumber, groupCalls } from '../src/lib/intakeGroups.js';

const OUR_LINE = '+18882600142';
const CALLER = '+12134317465';

function call(overrides) {
  return {
    id: 'icall_1',
    candidate_id: null,
    entry_point: 'dial_in',
    from_number: CALLER,
    to_number: OUR_LINE,
    contact_name: null,
    contact_email: null,
    status: 'extracted',
    review_needed: false,
    started_at: '2026-08-27T23:00:00Z',
    ...overrides,
  };
}

test('counterpartyNumber reads the other end, not our own line', () => {
  assert.equal(counterpartyNumber(call({})), CALLER);
  // On an outbound call from_number is the intake line; reading it as the
  // caller's is how our own number ends up on the card.
  assert.equal(
    counterpartyNumber(
      call({ entry_point: 'outbound', from_number: OUR_LINE, to_number: CALLER }),
    ),
    CALLER,
  );
});

test('repeat calls from one number collapse into a single group', () => {
  const groups = groupCalls([
    call({ id: 'a', started_at: '2026-08-27T23:16:49Z' }),
    call({ id: 'b', started_at: '2026-08-27T23:14:47Z' }),
    call({ id: 'c', started_at: '2026-08-27T23:14:34Z' }),
  ]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].calls.length, 3);
  // Newest first, and the group carries the newest timestamp.
  assert.deepEqual(
    groups[0].calls.map((c) => c.id),
    ['a', 'b', 'c'],
  );
  assert.equal(groups[0].latest, '2026-08-27T23:16:49Z');
});

test('a name captured on any call names the whole group', () => {
  const groups = groupCalls([
    call({ id: 'a', started_at: '2026-08-27T23:00:00Z' }),
    call({ id: 'b', contact_name: 'Tushar Gupta', started_at: '2026-08-27T22:00:00Z' }),
  ]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].contactName, 'Tushar Gupta');
});

test('an unattributed call folds into the candidate group for the same number', () => {
  const groups = groupCalls([
    call({ id: 'unattributed', started_at: '2026-08-27T23:00:00Z' }),
    call({ id: 'attributed', candidate_id: 'cand_1', started_at: '2026-08-27T22:00:00Z' }),
  ]);
  assert.equal(groups.length, 1, 'the same person must not appear twice');
  assert.equal(groups[0].candidateId, 'cand_1');
  assert.equal(groups[0].calls.length, 2);
});

test('an outbound call groups with the inbound calls from that person', () => {
  const groups = groupCalls([
    call({ id: 'in', started_at: '2026-08-27T23:00:00Z' }),
    call({
      id: 'out',
      entry_point: 'outbound',
      from_number: OUR_LINE,
      to_number: CALLER,
      started_at: '2026-08-27T22:00:00Z',
    }),
  ]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].number, CALLER);
});

test('different callers stay separate, newest group first', () => {
  const groups = groupCalls([
    call({ id: 'a', from_number: '+15125550188', started_at: '2026-08-27T20:00:00Z' }),
    call({ id: 'b', started_at: '2026-08-27T23:00:00Z' }),
  ]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].calls[0].id, 'b');
});

test('review counts roll up to the group', () => {
  const groups = groupCalls([
    call({ id: 'a', review_needed: true }),
    call({ id: 'b', review_needed: false }),
  ]);
  assert.equal(groups[0].reviewCount, 1);
});

test('an empty or missing list yields no groups', () => {
  assert.deepEqual(groupCalls([]), []);
  assert.deepEqual(groupCalls(undefined), []);
});
