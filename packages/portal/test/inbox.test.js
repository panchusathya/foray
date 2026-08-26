import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { chipsFor, kindLabel, order, senderInitials, senderLabel } from '../src/lib/inbox.js';

describe('chipsFor', () => {
  it('always offers All, even on an empty inbox', () => {
    const chips = chipsFor({});
    assert.equal(chips.length, 1);
    assert.equal(chips[0].key, 'all');
  });

  it('hides a filter with nothing behind it', () => {
    // A row of chips that all read zero is a row of dead controls.
    const chips = chipsFor({ all: 2, interview: 2 });
    assert.deepEqual(
      chips.map((c) => c.key),
      ['all', 'interview'],
    );
  });

  it('keeps the declared order, not the order counts arrived in', () => {
    const chips = chipsFor({ all: 3, interview: 1, verification: 1, rejected: 1 });
    assert.deepEqual(
      chips.map((c) => c.key),
      ['all', 'verification', 'rejected', 'interview'],
    );
  });
});

describe('senderLabel', () => {
  it('prefers the employer over the ATS robot that sent it', () => {
    // "Greenhouse Recruiting" says which vendor, not which employer.
    const label = senderLabel({ company: 'Parasail', from_name: 'Greenhouse Recruiting' });
    assert.equal(label, 'Parasail');
  });

  it('falls back to the display name when we matched no application', () => {
    assert.equal(senderLabel({ from_name: 'Goldman Sachs Recruiting' }), 'Goldman Sachs Recruiting');
  });

  it('falls back to the domain when there is no display name', () => {
    assert.equal(senderLabel({ from_address: 'no-reply@ashbyhq.com' }), 'ashbyhq.com');
  });

  it('never renders empty', () => {
    assert.equal(senderLabel({}), 'Unknown sender');
  });
});

describe('senderInitials', () => {
  it('takes one letter from each of the first two words', () => {
    assert.equal(senderInitials({ company: 'Goldman Sachs' }), 'GS');
  });

  it('takes two letters from a single word', () => {
    assert.equal(senderInitials({ company: 'Parasail' }), 'PA');
  });

  it('survives punctuation-only names', () => {
    assert.equal(senderInitials({ company: '—' }), '?');
  });
});

describe('kindLabel', () => {
  it('describes the ones a person acts on', () => {
    assert.equal(kindLabel('interview'), 'They want to talk');
    assert.equal(kindLabel('verification'), 'Verify your account');
  });

  it('says nothing for an ordinary message', () => {
    assert.equal(kindLabel('other'), '');
  });
});

describe('order', () => {
  it('puts the newest first', () => {
    const rows = order([
      { id: 'a', received_at: '2026-08-01T00:00:00Z' },
      { id: 'b', received_at: '2026-08-20T00:00:00Z' },
    ]);
    assert.deepEqual(rows.map((r) => r.id), ['b', 'a']);
  });

  it('breaks a tie by putting unread first', () => {
    const rows = order([
      { id: 'read', received_at: '2026-08-20T00:00:00Z', read: true },
      { id: 'unread', received_at: '2026-08-20T00:00:00Z', read: false },
    ]);
    assert.deepEqual(rows.map((r) => r.id), ['unread', 'read']);
  });

  it('does not mutate its input', () => {
    const input = [
      { id: 'a', received_at: '2026-08-01T00:00:00Z' },
      { id: 'b', received_at: '2026-08-20T00:00:00Z' },
    ];
    order(input);
    assert.equal(input[0].id, 'a');
  });

  it('tolerates a missing date rather than throwing', () => {
    const rows = order([{ id: 'a' }, { id: 'b', received_at: '2026-08-20T00:00:00Z' }]);
    assert.equal(rows[0].id, 'b');
  });
});
