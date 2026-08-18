/** JSONL and CSV file connectors — the workhorse for ATS/CRM exports. */

import { createReadStream } from 'node:fs';
import { createInterface } from 'node:readline';

import { log } from '../log.js';
import { toCandidate } from '../normalize.js';

/**
 * Stream a .jsonl file so a million-row export doesn't need a million-row heap.
 * @returns {AsyncGenerator<object>}
 */
export async function* readJsonl(path) {
  const reader = createInterface({
    input: createReadStream(path, { encoding: 'utf8' }),
    crlfDelay: Infinity,
  });
  let lineNumber = 0;
  for await (const line of reader) {
    lineNumber += 1;
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      yield JSON.parse(trimmed);
    } catch (error) {
      log.warn('skipping malformed jsonl line', { line: lineNumber, error: error.message });
    }
  }
}

/**
 * Minimal RFC4180 CSV parser: handles quoted fields, embedded commas, escaped
 * quotes and CRLF. Written out rather than pulled in so this package stays
 * dependency-free.
 */
export function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let inQuotes = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];

    if (inQuotes) {
      if (char === '"') {
        if (text[index + 1] === '"') {
          field += '"';
          index += 1;
        } else {
          inQuotes = false;
        }
      } else {
        field += char;
      }
      continue;
    }

    if (char === '"') {
      inQuotes = true;
    } else if (char === ',') {
      row.push(field);
      field = '';
    } else if (char === '\n') {
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
    } else if (char !== '\r') {
      field += char;
    }
  }

  if (field !== '' || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

export function csvToRecords(text) {
  const rows = parseCsv(text).filter((row) => row.some((cell) => cell.trim() !== ''));
  if (rows.length === 0) return [];
  const headers = rows[0].map((header) => header.trim());
  return rows.slice(1).map((row) =>
    Object.fromEntries(headers.map((header, index) => [header, row[index] ?? ''])),
  );
}

export async function* readCsv(path) {
  const { readFile } = await import('node:fs/promises');
  const text = await readFile(path, 'utf8');
  for (const record of csvToRecords(text)) {
    yield record;
  }
}

/** Read a file of either format and yield Juicebox candidate payloads. */
export async function* fileCandidates(path, { source } = {}) {
  const isCsv = path.toLowerCase().endsWith('.csv');
  const iterator = isCsv ? readCsv(path) : readJsonl(path);
  const resolvedSource = source ?? (isCsv ? 'csv-import' : 'jsonl-import');

  for await (const record of iterator) {
    const candidate = toCandidate(record, { source: resolvedSource });
    if (candidate) yield candidate;
    else log.warn('skipping unidentifiable record', { source: resolvedSource });
  }
}
