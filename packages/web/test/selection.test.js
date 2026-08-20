import assert from 'node:assert/strict';
import { describe, it, test } from 'node:test';

import {
  countOnPage,
  pageWindow,
  selectAllFrom,
  togglePage,
  toggleOne,
  toggleRange,
} from '../src/lib/selection.js';

const hits = (start, count) =>
  Array.from({ length: count }, (_, index) => ({ login: `candidate-${start + index}` }));

test('selecting and deselecting page two preserves page one', () => {
  const first = hits(1, 40);
  const second = hits(41, 40);
  let selected = togglePage(new Map(), first);
  selected = togglePage(selected, second);
  assert.equal(selected.size, 80);
  selected = togglePage(selected, second);
  assert.deepEqual([...selected.keys()], first.map((hit) => hit.login));
});

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

test('selectAllFrom retains every hit', () => {
  assert.equal(selectAllFrom(hits(1, 160)).size, 160);
});

test('countOnPage ignores selections from other pages', () => {
  const page = hits(41, 4);
  const selected = selectAllFrom([hits(1, 1)[0], ...page.slice(0, 2)]);
  assert.equal(countOnPage(selected, page), 2);
});

test('pageWindow stays bounded at the first, middle, and last page', () => {
  assert.deepEqual(pageWindow(0, 5), [0, 1, 2, 3, 4]);
  assert.deepEqual(pageWindow(0, 40), [0, 1, 2, 3, 4, 5, 6]);
  assert.deepEqual(pageWindow(20, 40), [17, 18, 19, 20, 21, 22, 23]);
  assert.deepEqual(pageWindow(39, 40), [33, 34, 35, 36, 37, 38, 39]);
});

describe('keying rows by something other than login', () => {
  // The Contacts grid selects candidates, which have an id and no login. The
  // extractor is what lets one set of helpers serve both surfaces.
  const byId = (row) => row.id;
  const rows = [{ id: 'cand_a' }, { id: 'cand_b' }, { id: 'cand_c' }];

  it('toggleOne keys on the extractor, not on login', () => {
    const one = toggleOne(new Map(), rows[0], byId);
    assert.deepEqual([...one.keys()], ['cand_a']);
    assert.equal(toggleOne(one, rows[0], byId).size, 0);
  });

  it('togglePage selects then clears the whole page', () => {
    const all = togglePage(new Map(), rows, byId);
    assert.deepEqual([...all.keys()], ['cand_a', 'cand_b', 'cand_c']);
    assert.equal(togglePage(all, rows, byId).size, 0);
  });

  it('toggleRange is inclusive under a custom key', () => {
    const range = toggleRange(new Map(), rows, 0, 1, byId);
    assert.deepEqual([...range.keys()], ['cand_a', 'cand_b']);
  });

  it('selectAllFrom and countOnPage honour the extractor', () => {
    const all = selectAllFrom(rows, byId);
    assert.equal(all.size, 3);
    assert.equal(countOnPage(all, rows, byId), 3);
  });

  it('defaults to login when no extractor is given, so Sourcing is unchanged', () => {
    const hits = [{ login: 'ada' }, { login: 'bo' }];
    assert.deepEqual([...selectAllFrom(hits).keys()], ['ada', 'bo']);
    assert.deepEqual([...toggleOne(new Map(), hits[0]).keys()], ['ada']);
    assert.equal(countOnPage(selectAllFrom(hits), hits), 2);
  });
});
