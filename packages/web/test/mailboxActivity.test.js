import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  ACTIVITY_WINDOW_HOURS,
  activityVisible,
  rowVisible,
} from '../src/lib/mailboxActivity.js';

const NOW = new Date('2026-09-22T08:00:00Z');
const ago = (hours) => new Date(NOW.getTime() - hours * 3600 * 1000).toISOString();

describe('activityVisible', () => {
  it('hides a mailbox that has never run anything', () => {
    assert.equal(activityVisible({ status: 'idle' }, NOW), false);
    assert.equal(activityVisible({}, NOW), false);
    assert.equal(activityVisible(undefined, NOW), false);
  });

  it('always shows work in flight', () => {
    assert.equal(activityVisible({ status: 'queued' }, NOW), true);
    assert.equal(activityVisible({ status: 'running' }, NOW), true);
  });

  it('keeps a failure up however old it is, because someone must act on it', () => {
    assert.equal(activityVisible({ status: 'failed', finished_at: ago(900) }, NOW), true);
    assert.equal(
      activityVisible({ status: 'done', error: 'token expired', finished_at: ago(900) }, NOW),
      true,
    );
  });

  it('shows a success for a day and then stops', () => {
    assert.equal(activityVisible({ status: 'done', finished_at: ago(1) }, NOW), true);
    assert.equal(
      activityVisible({ status: 'done', finished_at: ago(ACTIVITY_WINDOW_HOURS - 1) }, NOW),
      true,
    );
    assert.equal(
      activityVisible({ status: 'done', finished_at: ago(ACTIVITY_WINDOW_HOURS + 1) }, NOW),
      false,
    );
  });

  it('hides the August sweep that started this whole complaint', () => {
    // tushar@goforay.io, still on screen on 22 September.
    const sweep = { status: 'done', scanned: 346, found: 339, finished_at: '2026-08-20T14:54:38Z' };
    assert.equal(activityVisible(sweep, NOW), false);
  });

  it('treats done-with-no-finish-time as long over', () => {
    assert.equal(activityVisible({ status: 'done' }, NOW), false);
    assert.equal(activityVisible({ status: 'done', finished_at: 'not a date' }, NOW), false);
  });

  it('shows a status it does not recognise rather than swallowing it', () => {
    assert.equal(activityVisible({ status: 'paused-by-provider' }, NOW), true);
  });
});

describe('rowVisible', () => {
  const old = { status: 'done', finished_at: ago(900) };
  const fresh = { status: 'done', finished_at: ago(2) };

  it('is false only when both halves are stale', () => {
    assert.equal(rowVisible({ backfill: old, discovery: old }, NOW), false);
  });

  it('is true when either half is worth showing', () => {
    assert.equal(rowVisible({ backfill: fresh, discovery: old }, NOW), true);
    assert.equal(rowVisible({ backfill: old, discovery: { status: 'running' } }, NOW), true);
  });

  it('survives a row with nothing on it', () => {
    assert.equal(rowVisible({}, NOW), false);
    assert.equal(rowVisible(undefined, NOW), false);
  });
});
