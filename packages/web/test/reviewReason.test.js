import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { fieldLabel, parseReviewReason } from '../src/lib/reviewReason.js';

// The exact line off a real call, which is what prompted the rewrite.
const REAL = [
  'low confidence on active_processes, compensation, ok_to_share_with_clients, timeline,',
  'urgency_driver, work_authorization; required fields missing: spec.seniority, spec.locations,',
  'spec.remote_ok, compensation, spec.must_have_skills, timeline, employment_status,',
  'active_processes, ok_to_share_with_clients; no explicit permission to share with clients;',
  'caller not identified',
].join(' ');

describe('parseReviewReason', () => {
  it('splits a real reason into its separate concerns', () => {
    const groups = parseReviewReason(REAL);
    assert.deepEqual(
      groups.map((group) => group.title),
      [
        'Worth double-checking',
        'Never covered',
        'Cannot be shown to a client',
        'We do not know who called',
      ],
    );
  });

  it('names fields the way the agent asked about them', () => {
    const [lowConfidence] = parseReviewReason(REAL);
    assert.ok(lowConfidence.fields.includes('pay expectations'));
    assert.ok(lowConfidence.fields.includes('permission to share them'));
    // No raw keys survive into the rendered list.
    assert.ok(!lowConfidence.fields.some((field) => field.includes('_')));
  });

  it('marks a missing required field harder than a shaky one', () => {
    const [lowConfidence, missing] = parseReviewReason(REAL);
    assert.equal(lowConfidence.tone, 'partial');
    assert.equal(missing.tone, 'miss');
  });

  it('passes an unrecognised clause through rather than dropping it', () => {
    const groups = parseReviewReason('some future reason we have not met');
    assert.equal(groups.length, 1);
    assert.equal(groups[0].title, 'some future reason we have not met');
  });

  it('is empty for an empty reason', () => {
    assert.deepEqual(parseReviewReason(''), []);
    assert.deepEqual(parseReviewReason(undefined), []);
    assert.deepEqual(parseReviewReason('   '), []);
  });

  it('tells the two sharing-permission clauses apart', () => {
    const never = parseReviewReason('no explicit permission to share with clients');
    const claimed = parseReviewReason(
      'sharing permission read from the transcript, not yet confirmed',
    );
    assert.equal(never[0].tone, 'miss');
    assert.equal(claimed[0].tone, 'partial');
    assert.notEqual(never[0].title, claimed[0].title);
  });
});

describe('fieldLabel', () => {
  it('falls back readably for a key it has never seen', () => {
    assert.equal(fieldLabel('spec.some_new_thing'), 'some new thing');
    assert.equal(fieldLabel('brand_new_field'), 'brand new field');
  });
});
