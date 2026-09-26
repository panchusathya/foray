import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { newestOnly } from '../src/lib/race.js';

describe('newestOnly', () => {
  it('silences a request that a newer one has already superseded', () => {
    // Typing "Alexan" then "Alexandra": both are on the wire, and the shorter
    // prefix is the one that comes back last.
    const start = newestOnly();
    const alexan = start();
    const alexandra = start();

    assert.equal(alexandra(), true);
    assert.equal(alexan(), false);
  });

  it('gives each hook instance its own counter', () => {
    const a = newestOnly();
    const b = newestOnly();
    const first = a();
    b();
    // One component starting a request must not invalidate another's.
    assert.equal(first(), true);
  });
});
