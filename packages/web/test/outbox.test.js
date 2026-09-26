import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { localToIso, reenrolPayload } from '../src/lib/outbox.js';

describe('localToIso', () => {
  it('reads a datetime-local value in the local zone and answers in UTC', () => {
    const local = '2026-09-22T09:00';
    // The same rule the browser applies: no zone in the string means local.
    assert.equal(localToIso(local), new Date(local).toISOString());
    assert.ok(localToIso(local).endsWith('Z'));
  });
});

describe('reenrolPayload', () => {
  it('carries the picked time as UTC for at', () => {
    const payload = reenrolPayload({ ids: ['a'], when: 'at', at: '2026-09-24T10:15' });
    assert.equal(payload.when, 'at');
    assert.equal(payload.send_at, new Date('2026-09-24T10:15').toISOString());
  });
});
