// Pure selection and pager helpers for cross-page sourcing results.

export function toggleOne(selected, hit) {
  const next = new Map(selected);
  if (next.has(hit.login)) next.delete(hit.login);
  else next.set(hit.login, hit);
  return next;
}

export function togglePage(selected, pageHits) {
  const next = new Map(selected);
  const allSelected = pageHits.length > 0 && pageHits.every((hit) => next.has(hit.login));
  for (const hit of pageHits) {
    if (allSelected) next.delete(hit.login);
    else next.set(hit.login, hit);
  }
  return next;
}

export function toggleRange(selected, pageHits, from, to) {
  const next = new Map(selected);
  const start = Math.max(0, Math.min(from, to));
  const stop = Math.min(pageHits.length - 1, Math.max(from, to));
  for (let index = start; index <= stop; index += 1) {
    const hit = pageHits[index];
    if (hit) next.set(hit.login, hit);
  }
  return next;
}

export function selectAllFrom(hits) {
  return new Map(hits.map((hit) => [hit.login, hit]));
}

export function countOnPage(selected, pageHits) {
  return pageHits.reduce((count, hit) => count + Number(selected.has(hit.login)), 0);
}

export function pageWindow(pageIndex, pageCount, span = 7) {
  if (pageCount <= 0) return [];
  const width = Math.max(1, Math.min(span, pageCount));
  let start = Math.max(0, pageIndex - Math.floor(width / 2));
  start = Math.min(start, pageCount - width);
  return Array.from({ length: width }, (_, index) => start + index);
}
