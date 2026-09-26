import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { todoRows } from '../src/lib/todo.js';

describe('todoRows', () => {
  it('renders a key it has never seen instead of silently hiding it', () => {
    const [row] = todoRows([{ key: 'some_future_chore', count: 4 }]);
    assert.equal(row.title, 'some_future_chore');
    assert.equal(row.to, null);
  });
});
