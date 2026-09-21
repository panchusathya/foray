import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  REENROL_WHEN,
  defaultPickTime,
  describeReenrol,
  localToIso,
  reenrolPayload,
  toLocalInput,
} from '../src/lib/outbox.js';

describe('REENROL_WHEN', () => {
  it("defaults to the step's usual time, which is what a stop usually meant", () => {
    assert.equal(REENROL_WHEN[0].id, 'next_slot');
    assert.deepEqual(
      REENROL_WHEN.map((option) => option.id).sort(),
      ['at', 'next_slot', 'now'],
    );
  });
});

describe('localToIso', () => {
  it('reads a datetime-local value in the local zone and answers in UTC', () => {
    const local = '2026-09-22T09:00';
    // The same rule the browser applies: no zone in the string means local.
    assert.equal(localToIso(local), new Date(local).toISOString());
    assert.ok(localToIso(local).endsWith('Z'));
  });

  it('gives null for nothing or nonsense rather than an Invalid Date', () => {
    assert.equal(localToIso(''), null);
    assert.equal(localToIso(undefined), null);
    assert.equal(localToIso('not a date'), null);
  });

  it('round-trips through toLocalInput', () => {
    const date = new Date(2026, 8, 22, 9, 30);
    assert.equal(toLocalInput(date), '2026-09-22T09:30');
    assert.equal(localToIso(toLocalInput(date)), date.toISOString());
  });
});

describe('defaultPickTime', () => {
  it('is tomorrow at nine in the morning, local', () => {
    const now = new Date(2026, 8, 21, 15, 47, 12);
    assert.equal(defaultPickTime(now), '2026-09-22T09:00');
  });

  it('rolls the month when tomorrow is in it', () => {
    assert.equal(defaultPickTime(new Date(2026, 8, 30, 23, 0)), '2026-10-01T09:00');
  });
});

describe('reenrolPayload', () => {
  it('sends only the ids and the mode for now and next_slot', () => {
    assert.deepEqual(reenrolPayload({ ids: ['a', 'b'], when: 'now' }), {
      enrollment_ids: ['a', 'b'],
      when: 'now',
      force: false,
    });
    assert.deepEqual(reenrolPayload({ ids: ['a'], when: 'next_slot', force: true }), {
      enrollment_ids: ['a'],
      when: 'next_slot',
      force: true,
    });
  });

  it('carries the picked time as UTC for at', () => {
    const payload = reenrolPayload({ ids: ['a'], when: 'at', at: '2026-09-24T10:15' });
    assert.equal(payload.when, 'at');
    assert.equal(payload.send_at, new Date('2026-09-24T10:15').toISOString());
  });

  it('refuses at without a time instead of sending a null the API would reject', () => {
    assert.throws(() => reenrolPayload({ ids: ['a'], when: 'at', at: '' }), /Pick a date/);
  });
});

describe('describeReenrol', () => {
  const format = (iso) => `@${iso}`;

  it('names the next send for a scheduled re-enrol', () => {
    const text = describeReenrol(
      { resumed: 2, completed: 0, sent: 0, not_found: 0, skipped: [], next_send_at: 'T' },
      'next_slot',
      format,
    );
    assert.equal(text, 'Re-enrolled 2 — next email @T.');
  });

  it('reports what went out for now, and what is still waiting', () => {
    assert.equal(
      describeReenrol({ resumed: 2, sent: 2, not_found: 0, skipped: [] }, 'now', format),
      'Re-enrolled 2 — 2 sent.',
    );
    assert.equal(
      describeReenrol({ resumed: 3, sent: 1, not_found: 0, skipped: [] }, 'now', format),
      'Re-enrolled 3 — 1 sent, 2 due now and waiting on the sender.',
    );
  });

  it('says who was left stopped because they replied, and who had nothing left', () => {
    const text = describeReenrol(
      {
        resumed: 1,
        completed: 1,
        sent: 0,
        not_found: 1,
        skipped: [{ full_name: 'Ada Okonkwo', quiet_days: 30 }],
        next_send_at: null,
      },
      'next_slot',
      format,
    );
    assert.equal(
      text,
      'Re-enrolled 1. 1 had nothing left to send and is marked complete. ' +
        'Ada Okonkwo replied to you in the last 30 days and was left stopped. ' +
        '1 could not be re-enrolled.',
    );
  });

  it('never answers with an empty line', () => {
    assert.equal(describeReenrol({ resumed: 0, not_found: 0, skipped: [] }, 'now'), 'Nothing changed.');
  });
});
