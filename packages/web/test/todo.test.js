import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { ageLine, todoRows } from '../src/lib/todo.js';

describe('todoRows', () => {
  it('puts unanswered replies first — the only item with a person on the other end', () => {
    const rows = todoRows([
      { key: 'intake_review', count: 3 },
      { key: 'replies_waiting', count: 2, oldest_hours: 50 },
      { key: 'packages_failed', count: 1 },
    ]);
    assert.deepEqual(
      rows.map((row) => row.key),
      ['replies_waiting', 'packages_failed', 'intake_review'],
    );
  });

  it('drops zero counts rather than rendering "0 things to do"', () => {
    assert.deepEqual(todoRows([{ key: 'intake_review', count: 0 }]), []);
    assert.deepEqual(todoRows([]), []);
    assert.deepEqual(todoRows(undefined), []);
  });

  it('renders a key it has never seen instead of silently hiding it', () => {
    const [row] = todoRows([{ key: 'some_future_chore', count: 4 }]);
    assert.equal(row.title, 'some_future_chore');
    assert.equal(row.to, null);
  });

  it('agrees with itself about singular and plural', () => {
    const [one] = todoRows([{ key: 'replies_waiting', count: 1, oldest_hours: 30 }]);
    const [many] = todoRows([{ key: 'replies_waiting', count: 5, oldest_hours: 30 }]);
    assert.match(one.title, /1 reply is waiting/);
    assert.match(many.title, /5 replies are waiting/);
  });
});

describe('ageLine', () => {
  it('says it in the units a person would', () => {
    assert.match(ageLine(30), /1 day/);
    assert.match(ageLine(96), /4 days/);
    assert.match(ageLine(24 * 21), /3 weeks/);
  });

  it('is empty when the server sent no age', () => {
    assert.equal(ageLine(null), '');
    assert.equal(ageLine(undefined), '');
  });
});
