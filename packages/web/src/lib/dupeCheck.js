// "Which of these people are already in the CRM?" for a file that is NOT going
// to be imported -- a list somebody wants to hand on, cleaned of everyone we
// already know.
//
// Pure and dependency-free, like csvImport.js, so the decisions worth
// asserting (which column means what, which rows go back out) need no DOM.
// The matching itself is the server's `duplicates.known_contacts` behind
// POST /v1/imports/known -- the same email / LinkedIn / name-and-company
// evidence the importer's "already in contacts" column uses, so the two
// screens cannot disagree about who is a duplicate. That route writes nothing,
// and neither does anything here.
//
// The difference from the importer: that one maps a Juicebox export onto a
// candidate payload, so it knows its column names. This takes whatever file
// somebody has, so every column is a guess the recruiter can override, and the
// file that comes back out is their own -- every column, every cell, in the
// order they gave it -- minus the rows that matched.

import { normalizeEmail } from './csvImport.js';

// The server's own ceiling per request (imports.MAX_KNOWN).
export const KNOWN_CHUNK = 5000;
// Matches KnownPerson's cap on addresses per row.
const MAX_EMAILS = 10;

// Option values for the "Matching on" selects. A column is its index, as a
// string, because a file can repeat a header name and the select still has to
// tell the two apart.
export const NONE = '';
export const AUTO = 'auto';
export const SPLIT_NAME = 'split';

function canonical(header) {
  return String(header ?? '').trim().toLowerCase().replace(/[\s_]+/g, ' ');
}

function firstExact(headers, aliases) {
  for (const alias of aliases) {
    const index = headers.findIndex((h) => canonical(h) === alias);
    if (index >= 0) return index;
  }
  return -1;
}

const FULL_NAME = ['name', 'full name', 'candidate name', 'contact name', 'person name'];
const FIRST_NAME = ['first name', 'firstname', 'first', 'given name'];
const LAST_NAME = ['last name', 'lastname', 'last', 'surname', 'family name'];
const COMPANY = [
  'current company',
  'company',
  'company name',
  'current org name',
  'current employer',
  'employer',
  'organization',
  'organisation',
  'account name',
];
// A column about an address rather than holding one: "Email Status",
// "Work Email Verification". Its cells are words like "deliverable", which
// would never parse as an address anyway, but excluding it keeps the
// "Matching on" line honest about which columns are actually being read.
const NOT_AN_ADDRESS = /status|verif|valid|type|source|confidence|score|opt/;

/** Best guess at which column means what. Every role is optional. */
export function guessColumns(headers = []) {
  const full = firstExact(headers, FULL_NAME);
  const first = firstExact(headers, FIRST_NAME);
  const last = firstExact(headers, LAST_NAME);
  let company = firstExact(headers, COMPANY);
  if (company < 0) {
    company = headers.findIndex((h) => /company|employer|organi[sz]ation/.test(canonical(h)));
  }
  const linkedin = headers
    .map((h, index) => (canonical(h).includes('linkedin') ? index : -1))
    .filter((index) => index >= 0);
  const emails = headers
    .map((h, index) => {
      const name = canonical(h);
      return /e-?mail/.test(name) && !NOT_AN_ADDRESS.test(name) ? index : -1;
    })
    .filter((index) => index >= 0);
  return { full, first, last, company, linkedin, emails };
}

/** The selects' starting values, from a guess. */
export function defaultMapping(guess) {
  let name = NONE;
  if (guess.full >= 0) name = String(guess.full);
  else if (guess.first >= 0 && guess.last >= 0) name = SPLIT_NAME;
  else if (guess.first >= 0) name = String(guess.first);
  return {
    name,
    company: guess.company >= 0 ? String(guess.company) : NONE,
    // AUTO reads every column that looks like one, and when none does, any
    // cell that is nothing but an address or a profile URL -- see cellsFor.
    linkedin: AUTO,
    email: AUTO,
  };
}

const LINKEDIN_CELL = /^(https?:\/\/)?([a-z]{2,3}\.)?linkedin\.com\/in\/[^\s/?#]+\/?(\?\S*)?$/i;
const EMAIL_CELL = /^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$/;

// Which cells a role reads for one row. In AUTO with no matching header, only a
// cell that is *entirely* an address or a profile URL counts: a notes column
// saying "referred by bob@acme.com" must not make this row Bob.
function cellsFor(cells, choice, headerIndices, wholeCell) {
  if (choice === NONE) return [];
  if (choice !== AUTO) return [cells[Number(choice)] ?? ''];
  if (headerIndices.length) return headerIndices.map((index) => cells[index] ?? '');
  return cells.filter((cell) => wholeCell.test(String(cell ?? '').trim()));
}

function nameFor(cells, mapping, guess) {
  if (mapping.name === NONE) return '';
  if (mapping.name === SPLIT_NAME) {
    const first = (cells[guess.first] ?? '').trim();
    const last = (cells[guess.last] ?? '').trim();
    return `${first} ${last}`.trim();
  }
  return (cells[Number(mapping.name)] ?? '').trim();
}

/** What one row of the file can be recognised by. */
export function identityFor(cells, mapping, guess) {
  const emails = [];
  for (const cell of cellsFor(cells, mapping.email, guess.emails, EMAIL_CELL)) {
    // A cell can hold "a@x.com; b@y.com". normalizeEmail takes one address.
    for (const part of String(cell ?? '').split(/[,;\s]+/)) {
      const address = normalizeEmail(part);
      if (address && !emails.includes(address)) emails.push(address);
    }
  }
  const linkedin =
    cellsFor(cells, mapping.linkedin, guess.linkedin, LINKEDIN_CELL)
      .map((cell) => String(cell ?? '').trim())
      .find((cell) => /linkedin\.com\/in\//i.test(cell)) ?? '';
  return {
    emails: emails.slice(0, MAX_EMAILS),
    // Sent raw: the server folds it with enrich.linkedin_url.linkedin_slug,
    // which is not quite csvImport.linkedinSlug (see AGENTS.md), and folding
    // on both sides is how a match silently misses.
    linkedin_url: linkedin,
    full_name: nameFor(cells, mapping, guess),
    company: mapping.company === NONE ? '' : (cells[Number(mapping.company)] ?? '').trim(),
  };
}

// The server never matches on a name alone -- there are four Priya Ramans --
// so a row needs an address, a profile, or a name *with* a company.
export function isCheckable(identity) {
  return Boolean(
    identity.emails.length || identity.linkedin_url || (identity.full_name && identity.company),
  );
}

/** One entry per data row, in file order. `key` is the row's position. */
export function buildCheckRows(cells, mapping, guess) {
  return cells.map((row, index) => {
    const identity = identityFor(row, mapping, guess);
    return {
      key: String(index),
      index,
      cells: row,
      identity,
      checkable: isCheckable(identity),
      match: null,
    };
  });
}

export function lookupPeople(rows) {
  return rows.filter((row) => row.checkable).map((row) => ({ key: row.key, ...row.identity }));
}

export function chunkPeople(people, size = KNOWN_CHUNK) {
  const out = [];
  for (let i = 0; i < people.length; i += size) out.push(people.slice(i, i + size));
  return out;
}

/** New rows with each server match attached; never mutates. */
export function applyMatches(rows, matches) {
  const byKey = new Map((matches ?? []).map((match) => [match.key, match]));
  return rows.map((row) => {
    const match = byKey.get(row.key);
    return {
      ...row,
      match: match
        ? {
            candidateId: match.candidate_id,
            fullName: match.full_name,
            matchedOn: match.matched_on,
            reason: match.reason,
          }
        : null,
    };
  });
}

export function summarize(rows, removed) {
  const total = rows.length;
  const matched = rows.filter((row) => row.match).length;
  const weak = rows.filter((row) => row.match?.matchedOn === 'name_and_company').length;
  const unchecked = rows.filter((row) => !row.checkable).length;
  const removing = rows.filter((row) => removed.has(row.key)).length;
  return { total, matched, weak, unchecked, removing, keeping: total - removing };
}

// --------------------------------------------------------------------------
// writing the file back out
// --------------------------------------------------------------------------

function quoteCell(value, delimiter) {
  const text = value == null ? '' : String(value);
  const needs =
    text.includes(delimiter)
    || text.includes('"')
    || text.includes('\n')
    || text.includes('\r')
    || text !== text.trim();
  return needs ? `"${text.replace(/"/g, '""')}"` : text;
}

/**
 * The recruiter's own file, minus some rows. Cells are written exactly as they
 * came in -- no formula guard, no trimming -- because this is their data
 * going back to them, and the point is that it is the same file. The header
 * row, the delimiter and a leading BOM survive too.
 */
export function writeTable({ header, rows, delimiter = ',', bom = false }) {
  const lines = [header, ...rows].map((row) =>
    row.map((cell) => quoteCell(cell, delimiter)).join(delimiter),
  );
  return `${bom ? '﻿' : ''}${lines.join('\r\n')}\r\n`;
}

/** "candidates.xlsx" -> "candidates-deduped.csv". */
export function outputName(fileName, suffix, delimiter = ',') {
  const base = String(fileName || 'contacts').replace(/\.[^.]+$/, '') || 'contacts';
  return `${base}-${suffix}.${delimiter === '\t' ? 'tsv' : 'csv'}`;
}
