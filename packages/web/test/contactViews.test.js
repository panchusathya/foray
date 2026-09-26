import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  CONTACT_VIEWS,
  NO_REPLY_GRACE_DAYS,
  RECENTLY_ADDED_DAYS,
  countParams,
  resolveParams,
} from '../src/lib/contactViews.js';

// Fixed clock: every relative window below is asserted against this instant,
// so a boundary bug shows up as a failing date rather than a flaky test.
const NOW = new Date('2026-08-20T12:00:00.000Z');

function daysBefore(days) {
  return new Date(NOW.getTime() - days * 86400000).toISOString();
}

describe('resolveParams', () => {
  it('no reply excludes anyone emailed inside the grace period', () => {
    const params = resolveParams('no_reply', { now: NOW });
    assert.equal(params.relationship, 'never_replied');
    assert.equal(params.contacted_before, daysBefore(NO_REPLY_GRACE_DAYS));
  });

  it('recently added is a bounded window, not just a re-sort', () => {
    const params = resolveParams('recent', { now: NOW });
    assert.equal(params.created_after, daysBefore(RECENTLY_ADDED_DAYS));
    assert.equal(params.sort, 'created');
    assert.equal(params.dir, 'desc');
  });
});

describe('countParams', () => {
  it('agrees with resolveParams on every filter for every view', () => {
    // The count and the listing disagreeing is the bug this whole split
    // exists to prevent, so assert it structurally rather than per-view.
    for (const view of CONTACT_VIEWS) {
      const listed = resolveParams(view.id, { q: 'x', projectId: 'proj_1', now: NOW });
      const counted = countParams(view.id, { q: 'x', projectId: 'proj_1', now: NOW });
      for (const [key, value] of Object.entries(counted)) {
        assert.deepEqual(value, listed[key], `${view.id}.${key}`);
      }
    }
  });
});
