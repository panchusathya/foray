import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  CONTACT_VIEWS,
  DEFAULT_STALE_DAYS,
  NO_REPLY_GRACE_DAYS,
  PAGE_SIZE,
  RECENTLY_ADDED_DAYS,
  countParams,
  resolveParams,
  viewById,
} from '../src/lib/contactViews.js';

// Fixed clock: every relative window below is asserted against this instant,
// so a boundary bug shows up as a failing date rather than a flaky test.
const NOW = new Date('2026-08-20T12:00:00.000Z');

function daysBefore(days) {
  return new Date(NOW.getTime() - days * 86400000).toISOString();
}

describe('viewById', () => {
  it('falls back to the first view for an unknown id', () => {
    assert.equal(viewById('nonsense').id, CONTACT_VIEWS[0].id);
  });

  it('resolves every declared view by its own id', () => {
    for (const view of CONTACT_VIEWS) {
      assert.equal(viewById(view.id).id, view.id);
    }
  });
});

describe('resolveParams', () => {
  it('defaults to the view own sort and direction', () => {
    const params = resolveParams('going_cold', { now: NOW });
    assert.equal(params.sort, 'last_contacted');
    // Coldest first is the whole point of the view.
    assert.equal(params.dir, 'asc');
  });

  it('lets an explicit sort override the view default', () => {
    const params = resolveParams('going_cold', { sort: 'name', dir: 'desc', now: NOW });
    assert.equal(params.sort, 'name');
    assert.equal(params.dir, 'desc');
  });

  it('pages by PAGE_SIZE', () => {
    assert.equal(resolveParams('crm', { page: 0, now: NOW }).offset, 0);
    assert.equal(resolveParams('crm', { page: 3, now: NOW }).offset, 3 * PAGE_SIZE);
    assert.equal(resolveParams('crm', { now: NOW }).limit, PAGE_SIZE);
  });

  it('omits q entirely when the search box is empty', () => {
    assert.ok(!('q' in resolveParams('crm', { q: '', now: NOW })));
    assert.equal(resolveParams('crm', { q: 'ada', now: NOW }).q, 'ada');
  });

  it('scopes the CRM views and only the pool view to everything', () => {
    assert.equal(resolveParams('crm', { now: NOW }).scope, 'crm');
    assert.equal(resolveParams('going_cold', { now: NOW }).scope, 'crm');
    assert.equal(resolveParams('all', { now: NOW }).scope, 'all');
    assert.equal(resolveParams('voice_intake', { now: NOW }).scope, 'voice_intake');
  });

  it('going cold asks for the stale relationship at 90 days', () => {
    const params = resolveParams('going_cold', { now: NOW });
    assert.equal(params.relationship, 'stale');
    assert.equal(params.stale_days, DEFAULT_STALE_DAYS);
  });

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

  it('does not put a date window on views that have none', () => {
    for (const id of ['crm', 'going_cold', 'replied', 'all']) {
      const params = resolveParams(id, { now: NOW });
      assert.ok(!('created_after' in params), id);
    }
    assert.ok(!('contacted_before' in resolveParams('crm', { now: NOW })));
  });
});

describe('project scope', () => {
  it('narrows the views that are about a mandate', () => {
    for (const id of ['crm', 'going_cold', 'no_reply', 'replied', 'recent']) {
      assert.equal(resolveParams(id, { projectId: 'proj_1', now: NOW }).project_id, 'proj_1', id);
    }
  });

  it('leaves the workspace-wide views alone', () => {
    // Their blurbs promise everything; sending a project would make them lie.
    for (const id of ['all', 'voice_intake']) {
      const params = resolveParams(id, { projectId: 'proj_1', now: NOW });
      assert.ok(!('project_id' in params), id);
    }
  });

  it('omits the param entirely when nothing is scoped', () => {
    assert.ok(!('project_id' in resolveParams('crm', { now: NOW })));
    assert.ok(!('project_id' in resolveParams('crm', { projectId: null, now: NOW })));
  });

  it('composes with the relationship filter that answers the question', () => {
    // "Who replied to this project's outreach" is both filters at once.
    const params = resolveParams('replied', { projectId: 'proj_1', now: NOW });
    assert.equal(params.relationship, 'replied');
    assert.equal(params.project_id, 'proj_1');
    assert.equal(params.scope, 'crm');
  });

  it('declares a scope decision for every view', () => {
    // A view added without the flag would silently default to unscoped.
    for (const view of CONTACT_VIEWS) {
      assert.equal(typeof view.projectScoped, 'boolean', view.id);
    }
  });
});

describe('countParams', () => {
  it('drops paging and ordering but keeps every filter', () => {
    const params = countParams('no_reply', { q: 'ada', page: 2, now: NOW });

    for (const dropped of ['limit', 'offset', 'sort', 'dir']) {
      assert.ok(!(dropped in params), dropped);
    }
    assert.equal(params.q, 'ada');
    assert.equal(params.scope, 'crm');
    assert.equal(params.relationship, 'never_replied');
    assert.equal(params.contacted_before, daysBefore(NO_REPLY_GRACE_DAYS));
  });

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
