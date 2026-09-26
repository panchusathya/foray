import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { parseReviewReason } from '../src/lib/reviewReason.js';

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

  it('passes an unrecognised clause through rather than dropping it', () => {
    const groups = parseReviewReason('some future reason we have not met');
    assert.equal(groups.length, 1);
    assert.equal(groups[0].title, 'some future reason we have not met');
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
