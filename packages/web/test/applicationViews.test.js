import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  APPLICATION_STATUSES,
  APPLICATION_TRANSITIONS,
  applicationStatusTone,
} from '../src/lib/api.js';
import { APPLICATION_VIEWS, applicationView, inView } from '../src/lib/applicationViews.js';

describe('applicationView', () => {
  it('falls back to the first view for an unknown id', () => {
    assert.equal(applicationView('nonsense').id, APPLICATION_VIEWS[0].id);
    assert.equal(applicationView(undefined).id, APPLICATION_VIEWS[0].id);
  });

  it('defaults to the view that needs attention first', () => {
    assert.equal(APPLICATION_VIEWS[0].id, 'open');
  });
});

describe('inView', () => {
  const rows = APPLICATION_STATUSES.map((status) => ({ status }));

  it('the all view holds everything', () => {
    const all = applicationView('all');
    assert.equal(rows.filter((row) => inView(all, row)).length, rows.length);
  });

  it('closed and in-flight do not overlap', () => {
    const open = applicationView('open');
    const closed = applicationView('closed');
    const both = rows.filter((row) => inView(open, row) && inView(closed, row));
    assert.deepEqual(both, [], 'a row cannot be both in flight and closed');
  });

  it('every status lands in at least one view besides all', () => {
    const views = APPLICATION_VIEWS.filter((view) => view.id !== 'all');
    for (const row of rows) {
      const hit = views.some((view) => inView(view, row));
      assert.ok(hit, `${row.status} is invisible outside the All tab`);
    }
  });

  it('needs-the-candidate is exactly the two states waiting on a person', () => {
    const review = applicationView('review');
    const matched = rows.filter((row) => inView(review, row)).map((row) => row.status);
    assert.deepEqual(matched, ['prepared', 'awaiting_consent']);
  });
});

describe('APPLICATION_TRANSITIONS', () => {
  // Mirrors applications/state.py. The server is the authority and answers
  // 409, but a UI offering a move that cannot work wastes a click and reads
  // as a bug -- so the two tables have to agree.
  it('covers every status', () => {
    for (const status of APPLICATION_STATUSES) {
      assert.ok(
        Object.prototype.hasOwnProperty.call(APPLICATION_TRANSITIONS, status),
        `${status} has no transition list`,
      );
    }
  });

  it('only ever targets a real status', () => {
    for (const [from, targets] of Object.entries(APPLICATION_TRANSITIONS)) {
      for (const to of targets) {
        assert.ok(APPLICATION_STATUSES.includes(to), `${from} -> ${to} is not a status`);
      }
    }
  });

  it('terminal states are terminal', () => {
    assert.deepEqual(APPLICATION_TRANSITIONS.rejected, []);
    assert.deepEqual(APPLICATION_TRANSITIONS.withdrawn, []);
  });

  it('never offers a move back into draft from a sent state', () => {
    for (const sent of ['submitted', 'acknowledged', 'interviewing', 'offer']) {
      assert.ok(
        !APPLICATION_TRANSITIONS[sent].includes('draft'),
        `${sent} must not rewind to draft`,
      );
    }
  });
});

describe('applicationStatusTone', () => {
  it('uses tones the stylesheet actually defines', () => {
    const allowed = new Set(['strong', 'partial', 'miss', '']);
    for (const status of APPLICATION_STATUSES) {
      assert.ok(allowed.has(applicationStatusTone(status)), `${status} has an unknown tone`);
    }
  });

  it('reads forward motion as strong and closure as miss', () => {
    assert.equal(applicationStatusTone('interviewing'), 'strong');
    assert.equal(applicationStatusTone('awaiting_consent'), 'partial');
    assert.equal(applicationStatusTone('withdrawn'), 'miss');
  });
});
