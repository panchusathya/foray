import assert from 'node:assert/strict';
import { beforeEach, describe, it } from 'node:test';

import { once, resetOnce } from '../src/lib/once.js';

describe('once', () => {
  beforeEach(() => resetOnce());

  it('runs the factory once for a repeated key', async () => {
    let calls = 0;
    const factory = () => {
      calls += 1;
      return Promise.resolve(calls);
    };
    const [a, b] = await Promise.all([once('t', factory), once('t', factory)]);
    assert.equal(calls, 1, 'a single-use grant must be spent at most once');
    assert.equal(a, 1);
    assert.equal(b, 1, 'the second caller reads the first result');
  });

  it('does not exchange twice under a StrictMode double mount', async () => {
    // The real shape of the bug: the effect runs, is torn down, and runs
    // again synchronously. Guarding only the state update let both requests
    // reach the server, and the second 401 was what the candidate saw.
    const sent = [];
    const exchange = (token) => {
      sent.push(token);
      return sent.length === 1
        ? Promise.resolve({ access_token: 'ok' })
        : Promise.reject(new Error('401 link already used'));
    };
    const mount = () => once('tok', () => exchange('tok'));
    const first = mount();
    const second = mount();
    assert.deepEqual(sent, ['tok']);
    assert.deepEqual(await first, { access_token: 'ok' });
    assert.deepEqual(await second, { access_token: 'ok' });
  });

  it('keeps separate keys separate', async () => {
    const seen = [];
    await Promise.all(
      ['a', 'b'].map((key) => once(key, () => Promise.resolve(seen.push(key)))),
    );
    assert.deepEqual(seen, ['a', 'b']);
  });

  it('caches a rejection rather than retrying a spent grant', async () => {
    let calls = 0;
    const factory = () => {
      calls += 1;
      return Promise.reject(new Error('nope'));
    };
    await assert.rejects(() => once('bad', factory));
    await assert.rejects(() => once('bad', factory));
    assert.equal(calls, 1, 'a failed exchange may already have spent the grant');
  });
});
