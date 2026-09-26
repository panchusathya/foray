import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { formatPhone, telHref } from '../src/lib/phone.js';

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

  it('does not mangle something that is not E.164', () => {
    assert.equal(formatPhone('(415) 555-0123'), '(415) 555-0123');
  });
});

describe('telHref', () => {
  it('dials the digits, not the presentation', () => {
    assert.equal(telHref('+1 (415) 555-0123'), 'tel:+14155550123');
  });
});
