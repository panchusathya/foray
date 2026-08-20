// Pure selection and pager helpers for cross-page result lists.
//
// Sourcing keys its rows by GitHub login; the Contacts grid keys them by
// candidate id. Rather than fork the helpers, each takes an optional key
// extractor whose default is the original `hit.login` behaviour — so every
// existing Sourcing call site keeps working untouched.

const byLogin = (row) => row.login;

export function toggleOne(selected, hit, key = byLogin) {
  const next = new Map(selected);
  const id = key(hit);
  if (next.has(id)) next.delete(id);
  else next.set(id, hit);
  return next;
}

export function togglePage(selected, pageHits, key = byLogin) {
  const next = new Map(selected);
  const allSelected = pageHits.length > 0 && pageHits.every((hit) => next.has(key(hit)));
  for (const hit of pageHits) {
    if (allSelected) next.delete(key(hit));
    else next.set(key(hit), hit);
  }
  return next;
}

export function toggleRange(selected, pageHits, from, to, key = byLogin) {
  const next = new Map(selected);
  const start = Math.max(0, Math.min(from, to));
  const stop = Math.min(pageHits.length - 1, Math.max(from, to));
  for (let index = start; index <= stop; index += 1) {
    const hit = pageHits[index];
    if (hit) next.set(key(hit), hit);
  }
  return next;
}

export function selectAllFrom(hits, key = byLogin) {
  return new Map(hits.map((hit) => [key(hit), hit]));
}

export function countOnPage(selected, pageHits, key = byLogin) {
  return pageHits.reduce((count, hit) => count + Number(selected.has(key(hit))), 0);
}

export function pageWindow(pageIndex, pageCount, span = 7) {
  if (pageCount <= 0) return [];
  const width = Math.max(1, Math.min(span, pageCount));
  let start = Math.max(0, pageIndex - Math.floor(width / 2));
  start = Math.min(start, pageCount - width);
  return Array.from({ length: width }, (_, index) => start + index);
}
