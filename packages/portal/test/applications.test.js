import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  COLUMNS,
  actionPrompt,
  ago,
  byRecency,
  columnFor,
  greeting,
  groupByColumn,
  initials,
  isLive,
  matchesQuery,
  statusLabel,
  statusTone,
  summarise,
  trackFor,
} from '../src/lib/applications.js';

test('every status the server can send has a candidate-facing label', () => {
  const statuses = [
    'draft',
    'prepared',
    'awaiting_consent',
    'submitting',
    'submitted',
    'acknowledged',
    'interviewing',
    'offer',
    'rejected',
    'withdrawn',
  ];
  for (const status of statuses) {
    assert.notEqual(statusLabel(status), status, `${status} renders raw`);
    assert.ok(columnFor(status), `${status} has no column`);
  }
});

test('an unknown status degrades instead of throwing', () => {
  assert.equal(statusLabel('teleported'), 'teleported');
  assert.equal(statusTone('teleported'), '');
  assert.equal(columnFor('teleported'), null);
  assert.equal(trackFor('teleported'), null);
});

test('only the statuses that block the candidate are gold', () => {
  assert.equal(statusTone('prepared'), 'needs');
  assert.equal(statusTone('awaiting_consent'), 'needs');
  assert.equal(statusTone('submitted'), '');
  assert.equal(statusTone('offer'), 'good');
  assert.equal(statusTone('rejected'), 'done');
});

test('only an in-flight submission pulses', () => {
  assert.equal(isLive('submitting'), true);
  assert.equal(isLive('interviewing'), false);
});

test('the prompt for an approved application depends on who can send it', () => {
  const base = { status: 'awaiting_consent' };
  assert.match(actionPrompt({ ...base, can_auto_submit: true }), /have us send it/);
  assert.match(actionPrompt({ ...base, can_auto_submit: false }), /employer's site/);
  assert.equal(actionPrompt({ status: 'submitted' }), '');
  assert.equal(actionPrompt(null), '');
});

test('grouping keeps every column, including the empty ones', () => {
  const groups = groupByColumn([
    { id: '1', status: 'prepared' },
    { id: '2', status: 'submitted' },
    { id: '3', status: 'acknowledged' },
  ]);
  assert.equal(groups.length, COLUMNS.length);
  assert.deepEqual(
    groups.map((column) => column.items.length),
    [0, 1, 2, 0, 0, 0],
  );
});

test('an unknown status is dropped from the board rather than crashing it', () => {
  const groups = groupByColumn([{ id: '1', status: 'teleported' }]);
  assert.equal(
    groups.reduce((total, column) => total + column.items.length, 0),
    0,
  );
});

test('the track marks the current stage and nothing beyond it', () => {
  const track = trackFor('submitted');
  assert.deepEqual(
    track.map((step) => step.state),
    ['done', 'done', 'now', 'todo', 'todo'],
  );
});

test('a closed application gets no progress bar to guess with', () => {
  assert.equal(trackFor('rejected'), null);
  assert.equal(trackFor('withdrawn'), null);
});

test('summarise counts each application once', () => {
  const stats = summarise([
    { status: 'prepared' },
    { status: 'awaiting_consent' },
    { status: 'submitted' },
    { status: 'interviewing' },
    { status: 'offer' },
    { status: 'rejected' },
    { status: 'withdrawn' },
  ]);
  assert.equal(stats.total, 7);
  assert.equal(stats.needsYou, 2);
  assert.equal(stats.sent, 1);
  assert.equal(stats.interviewing, 1);
  assert.equal(stats.offers, 1);
  assert.equal(stats.closed, 2);
  assert.equal(stats.live, 5);
});

test('recency sorts on updated_at, newest first', () => {
  const rows = [
    { id: 'old', updated_at: '2026-01-01T00:00:00Z' },
    { id: 'new', updated_at: '2026-06-01T00:00:00Z' },
    { id: 'mid', updated_at: '2026-03-01T00:00:00Z' },
  ];
  assert.deepEqual(
    byRecency(rows).map((row) => row.id),
    ['new', 'mid', 'old'],
  );
  // Non-destructive: the tracker re-sorts a list it does not own.
  assert.equal(rows[0].id, 'old');
});

test('search covers title, company and location', () => {
  const row = { title: 'Staff Engineer', company: 'Stripe', location: 'Remote' };
  assert.equal(matchesQuery(row, 'stripe'), true);
  assert.equal(matchesQuery(row, 'STAFF'), true);
  assert.equal(matchesQuery(row, 'remote'), true);
  assert.equal(matchesQuery(row, ''), true);
  assert.equal(matchesQuery(row, 'databricks'), false);
});

test('initials survive punctuation, one-word names and nothing at all', () => {
  assert.equal(initials('Ada Lovelace'), 'AL');
  assert.equal(initials('Stripe, Inc.'), 'SI');
  assert.equal(initials('Anthropic'), 'AN');
  assert.equal(initials(''), '?');
  assert.equal(initials(null), '?');
  assert.equal(initials('!!!'), '?');
});

test('relative time reads in the units a person would use', () => {
  const now = Date.parse('2026-06-10T12:00:00Z');
  const at = (iso) => ago(iso, now);
  assert.equal(at('2026-06-10T11:59:30Z'), 'just now');
  assert.equal(at('2026-06-10T11:20:00Z'), '40m ago');
  assert.equal(at('2026-06-10T06:00:00Z'), '6h ago');
  assert.equal(at('2026-06-07T12:00:00Z'), '3d ago');
  assert.equal(at('2026-05-27T12:00:00Z'), '2w ago');
  assert.equal(at(''), '');
  assert.equal(at('not a date'), '');
});

test('the greeting follows the local hour', () => {
  const at = (hour) => greeting(new Date(2026, 5, 10, hour, 0, 0));
  assert.equal(at(8), 'Good morning');
  assert.equal(at(13), 'Good afternoon');
  assert.equal(at(21), 'Good evening');
});
