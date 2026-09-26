import assert from 'node:assert/strict';
import { test } from 'node:test';

import { toggleRange } from '../src/lib/selection.js';

const hits = (start, count) =>
  Array.from({ length: count }, (_, index) => ({ login: `candidate-${start + index}` }));

test('toggleRange is inclusive and page-local', () => {
  const page = hits(41, 5);
  const selected = toggleRange(new Map([["candidate-1", { login: 'candidate-1' }]]), page, 3, 1);
  assert.deepEqual([...selected.keys()], [
    'candidate-1',
    'candidate-42',
    'candidate-43',
    'candidate-44',
  ]);
});
