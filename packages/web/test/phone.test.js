import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { allPhones, formatPhone, primaryPhone, telHref } from '../src/lib/phone.js';

describe('formatPhone', () => {
  it('groups a North American number', () => {
    assert.equal(formatPhone('+14155550123'), '+1 (415) 555-0123');
  });

  it('leaves a number from anywhere else exactly as stored', () => {
    // A country code is one to three digits and the national grouping differs
    // by country, so guessing reformats a real London number into one that
    // looks wrong to anybody who knows it.
    assert.equal(formatPhone('+442079460958'), '+442079460958');
    assert.equal(formatPhone('+919820098200'), '+919820098200');
  });

  it('is empty for nothing', () => {
    assert.equal(formatPhone(''), '');
    assert.equal(formatPhone(null), '');
    assert.equal(formatPhone(undefined), '');
  });

  it('does not mangle something that is not E.164', () => {
    assert.equal(formatPhone('(415) 555-0123'), '(415) 555-0123');
  });
});

describe('primaryPhone', () => {
  it('formats the first number', () => {
    assert.equal(primaryPhone({ phones: ['+14155550123', '+14155559999'] }), '+1 (415) 555-0123');
  });

  it('is empty when the row has none, or no phones key at all', () => {
    assert.equal(primaryPhone({ phones: [] }), '');
    assert.equal(primaryPhone({}), '');
    assert.equal(primaryPhone(null), '');
  });
});

describe('allPhones', () => {
  it('formats every number for a tooltip', () => {
    assert.equal(
      allPhones({ phones: ['+14155550123', '+442079460958'] }),
      '+1 (415) 555-0123, +442079460958',
    );
  });
});

describe('telHref', () => {
  it('dials the digits, not the presentation', () => {
    assert.equal(telHref('+1 (415) 555-0123'), 'tel:+14155550123');
  });

  it('is empty for nothing, so a caller can skip the link', () => {
    assert.equal(telHref(''), '');
  });
});
