// Turns a Juicebox CSV export into CandidateIn payloads for /v1/candidates/bulk.
//
// Pure and dependency-free so it can be unit-tested without a browser, exactly
// like forayImport.js -- and for the same reason: the mapping decisions here
// (which address the sequencer will mail, which rows are excluded) are the ones
// worth asserting, and they should not need a DOM to check.
//
// Parsing happens in the browser rather than server-side on purpose. It means
// the recruiter sees every row, and picks which ones to take, BEFORE anything
// is written -- and it needs no upload endpoint, no multipart handling, and no
// half-imported file to clean up if they change their mind.

// --------------------------------------------------------------------------
// parsing
// --------------------------------------------------------------------------

// A real CSV parser, not a split(','). The Juicebox export quotes every field
// and its Location and Education columns contain commas ("San Francisco Bay
// Area, California, United States"), so a naive split silently shifts every
// column after the first comma -- which shows up as emails landing in the
// wrong field rather than as an error.
export function parseDelimited(text, { delimiter = '' } = {}) {
  const body = stripBom(text ?? '');
  if (!body.trim()) return { headers: [], rows: [] };
  const sep = delimiter || sniffDelimiter(body);

  const table = [];
  let row = [];
  let field = '';
  let quoted = false;
  let i = 0;

  while (i < body.length) {
    const ch = body[i];

    if (quoted) {
      if (ch === '"') {
        // A doubled quote inside a quoted field is one literal quote.
        if (body[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        quoted = false;
        i += 1;
        continue;
      }
      field += ch;
      i += 1;
      continue;
    }

    if (ch === '"' && field === '') {
      quoted = true;
      i += 1;
      continue;
    }
    if (ch === sep) {
      row.push(field);
      field = '';
      i += 1;
      continue;
    }
    if (ch === '\r' || ch === '\n') {
      // Treat CRLF as one terminator, and skip blank lines rather than
      // emitting a one-empty-field row for them.
      if (ch === '\r' && body[i + 1] === '\n') i += 1;
      row.push(field);
      field = '';
      if (row.some((cell) => cell !== '')) table.push(row);
      row = [];
      i += 1;
      continue;
    }
    field += ch;
    i += 1;
  }
  row.push(field);
  if (row.some((cell) => cell !== '')) table.push(row);

  const [headerRow = [], ...dataRows] = table;
  const headers = headerRow.map((h) => h.trim());
  const rows = dataRows.map((cells) => {
    const record = {};
    headers.forEach((header, index) => {
      record[header] = (cells[index] ?? '').trim();
    });
    return record;
  });
  return { headers, rows };
}

function stripBom(text) {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

// Decided on the header line only. Counting separators across the whole file
// would be swayed by commas inside quoted prose, which is the common case here.
function sniffDelimiter(body) {
  const firstLine = body.split(/\r?\n/, 1)[0] ?? '';
  const tabs = (firstLine.match(/\t/g) ?? []).length;
  const commas = (firstLine.match(/,/g) ?? []).length;
  return tabs > commas ? '\t' : ',';
}

// --------------------------------------------------------------------------
// column detection
// --------------------------------------------------------------------------

// Juicebox exports differ by which columns the user ticked -- one of the sample
// files has no email columns at all -- so every field is looked up by name and
// every one is optional. Matching is case- and space-insensitive because header
// casing is not a contract anyone promised us.
const COLUMNS = {
  firstName: ['first name', 'firstname'],
  lastName: ['last name', 'lastname'],
  fullName: ['name', 'full name', 'candidate name'],
  personalEmail: ['personal email', 'personal e-mail'],
  personalEmailStatus: ['personal email verification', 'personal email status'],
  workEmail: ['work email', 'work e-mail', 'business email'],
  workEmailStatus: ['work email verification', 'work email status'],
  email: ['email', 'e-mail', 'email address'],
  emailStatus: ['email verification', 'email status'],
  phones: ['phone numbers', 'phone number', 'phone'],
  phoneLineType: ['phone line type'],
  phoneVerification: ['phone verification status', 'phone verification'],
  location: ['location'],
  linkedin: ['linkedin', 'linkedin url', 'linkedin profile'],
  github: ['github', 'github url'],
  currentTitle: ['current title', 'title'],
  currentCompany: ['current org name', 'current company', 'company'],
  education: ['education'],
  totalExperience: ['total experience (years)', 'total experience'],
  profileUrl: ['juicebox profile url', 'profile url'],
};

function canonical(header) {
  return String(header ?? '').trim().toLowerCase().replace(/\s+/g, ' ');
}

export function detectColumns(headers = []) {
  const seen = new Map();
  for (const header of headers) seen.set(canonical(header), header);
  const found = {};
  for (const [role, aliases] of Object.entries(COLUMNS)) {
    for (const alias of aliases) {
      if (seen.has(alias)) {
        found[role] = seen.get(alias);
        break;
      }
    }
  }
  return found;
}

// Does this file carry any address at all? The answer changes what the page can
// offer, and saying so up front beats importing 79 people who can never be
// mailed.
export function hasEmailColumn(columns = {}) {
  return Boolean(columns.personalEmail || columns.workEmail || columns.email);
}

// --------------------------------------------------------------------------
// email selection
// --------------------------------------------------------------------------

// Juicebox's verification vocabulary, mapped onto the reach scores the
// sequencer already understands (`raw.*.email_reach`, gated by
// settings.min_reach_score). Deliberately the same mechanism Foray imports use
// rather than a second one.
export const VERIFICATION_SCORES = {
  deliverable: 90,
  risky: 40,
  undeliverable: 0,
};

export function verificationOf(value) {
  const key = String(value ?? '').trim().toLowerCase();
  return key in VERIFICATION_SCORES ? key : '';
}

// Undeliverable addresses are dropped here rather than left to the reach-score
// floor. FORAY_MIN_REACH_SCORE defaults to 0, and _sendable_address short-
// circuits on `floor <= 0` and returns emails[0] unconditionally -- so on a
// default install a known-bad address would be mailed. The floor is a second
// line of defence, not the first one.
export function selectEmails(row, columns) {
  const candidates = [];
  const push = (address, status, kind) => {
    const email = normalizeEmail(address);
    if (!email) return;
    const verification = verificationOf(status);
    if (verification === 'undeliverable') {
      candidates.push({ email, verification, kind, excluded: true });
      return;
    }
    candidates.push({ email, verification, kind, excluded: false });
  };

  // Personal before work, and that order is load-bearing rather than cosmetic:
  // emails[0] is the address send_step actually mails, and a work address in
  // front of a personal one puts a recruiting pitch in somebody's work inbox.
  push(row[columns.personalEmail], row[columns.personalEmailStatus], 'personal');
  push(row[columns.workEmail], row[columns.workEmailStatus], 'work');
  push(row[columns.email], row[columns.emailStatus], 'unknown');

  const deduped = [];
  const seen = new Set();
  for (const entry of candidates) {
    if (seen.has(entry.email)) continue;
    seen.add(entry.email);
    deduped.push(entry);
  }
  return {
    usable: deduped.filter((e) => !e.excluded),
    excluded: deduped.filter((e) => e.excluded),
  };
}

// Phone numbers, which the export can carry several of in one cell. Kept as
// written rather than normalised here: `enrich.normalize.clean_phones` tidies
// them for storage and `intake.phones.lookup_key` decides what is matchable,
// and that key function deliberately refuses to guess a country. Duplicating
// either rule in the browser is how the two drift apart.
//
// This matters for more than completeness: `candidate_phones` is what lets an
// inbound call be attributed to a person, so a candidate imported without a
// number can never be recognised when they ring back.
export function selectPhones(row, columns) {
  const cell = String(row[columns.phones] ?? '').trim();
  if (!cell) return [];
  const out = [];
  for (const part of cell.split(/[,;|]+/)) {
    const value = part.trim();
    // Six digits is below any real subscriber number and is usually a stray
    // extension or a spreadsheet artefact.
    if (!value || (value.match(/\d/g) ?? []).length < 7) continue;
    if (!out.includes(value)) out.push(value);
  }
  return out;
}

export function normalizeEmail(value) {
  const trimmed = String(value ?? '').trim().toLowerCase();
  if (!trimmed || !trimmed.includes('@')) return '';
  // One address per cell in every export seen so far, but a separator would
  // otherwise become part of the address and fail server-side validation.
  const first = trimmed.split(/[,;\s]+/).find((part) => part.includes('@')) ?? '';
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(first) ? first : '';
}

// --------------------------------------------------------------------------
// row -> CandidateIn
// --------------------------------------------------------------------------

export function rowName(row, columns) {
  const full = (row[columns.fullName] ?? '').trim();
  if (full) return full;
  const first = (row[columns.firstName] ?? '').trim();
  const last = (row[columns.lastName] ?? '').trim();
  return `${first} ${last}`.trim();
}

// A stable key so re-uploading an overlapping export updates rather than
// duplicates -- the same free idempotency forayImport.js gets from the GitHub
// login. LinkedIn first because it is present on every row of every sample
// export; the Juicebox profile URL is a fine second, and a name-and-company
// slug is the last resort for a file carrying neither.
export function sourceIdFor(row, columns) {
  const linkedin = linkedinSlug(row[columns.linkedin]);
  if (linkedin) return `li:${linkedin}`;
  const profile = (row[columns.profileUrl] ?? '').trim();
  if (profile) return `jb:${profile}`;
  const email = normalizeEmail(row[columns.personalEmail])
    || normalizeEmail(row[columns.workEmail])
    || normalizeEmail(row[columns.email]);
  if (email) return `email:${email}`;
  const name = rowName(row, columns).toLowerCase().replace(/\s+/g, '-');
  const company = (row[columns.currentCompany] ?? '').toLowerCase().replace(/\s+/g, '-');
  return name ? `name:${name}${company ? `@${company}` : ''}` : '';
}

// Strip protocol, host, query and trailing slash so the same profile written
// three ways yields one key.
export function linkedinSlug(url) {
  const raw = String(url ?? '').trim();
  if (!raw) return '';
  const match = raw.match(/linkedin\.com\/in\/([^/?#]+)/i);
  if (match) return decodeURIComponent(match[1]).toLowerCase();
  return raw.includes('linkedin.com') ? '' : raw.replace(/\/+$/, '').toLowerCase();
}

function parseYears(value) {
  const n = Number.parseFloat(String(value ?? '').replace(/[^\d.]/g, ''));
  return Number.isFinite(n) && n > 0 ? n : null;
}

export const CSV_SOURCE = 'juicebox_csv';

export function juiceboxRowToCandidate(row, columns) {
  const { usable, excluded } = selectEmails(row, columns);
  const emails = usable.map((e) => e.email);
  const phones = selectPhones(row, columns);

  // Only scored addresses go in email_reach: _sendable_address treats an
  // unscored address as passing, "because the gate only applies when we
  // actually scored them", and inventing a score for a blank verification
  // column would turn silence into a claim.
  const emailReach = {};
  for (const entry of usable) {
    if (entry.verification) emailReach[entry.email] = VERIFICATION_SCORES[entry.verification];
  }

  const payload = {
    source: CSV_SOURCE,
    source_id: sourceIdFor(row, columns),
    full_name: rowName(row, columns),
    location: (row[columns.location] ?? '').trim(),
    linkedin_url: (row[columns.linkedin] ?? '').trim(),
    github_url: (row[columns.github] ?? '').trim(),
    current_title: (row[columns.currentTitle] ?? '').trim(),
    current_company: (row[columns.currentCompany] ?? '').trim(),
    emails,
    phones,
    raw: {
      juicebox_csv: {
        email_reach: emailReach,
        // Kept so a later "why was this one skipped?" is answerable from the
        // record instead of from the original file.
        excluded_emails: excluded.map((e) => ({ email: e.email, reason: e.verification })),
        education: (row[columns.education] ?? '').trim(),
        profile_url: (row[columns.profileUrl] ?? '').trim(),
        // Not acted on, but the only record of why a number might be a bad
        // one to call -- and it costs nothing to keep.
        phone_line_type: (row[columns.phoneLineType] ?? '').trim(),
        phone_verification: (row[columns.phoneVerification] ?? '').trim(),
      },
    },
  };

  // years_experience must be omitted rather than sent as 0 when unknown:
  // normalize_candidate only computes it from experiences when it arrives as
  // None, so a 0 here permanently zeroes a value we could have derived.
  const years = parseYears(row[columns.totalExperience]);
  if (years !== null) payload.years_experience = years;

  return payload;
}

// --------------------------------------------------------------------------
// what the page needs to show
// --------------------------------------------------------------------------

// One pass over the file producing everything the review table renders, so the
// row list and the counts can never disagree.
export function buildImportRows(rows, columns) {
  return rows.map((row, index) => {
    const { usable, excluded } = selectEmails(row, columns);
    const name = rowName(row, columns);
    const sourceId = sourceIdFor(row, columns);
    let blocked = '';
    if (!name) blocked = 'no name';
    else if (!sourceId) blocked = 'nothing to identify them by';
    else if (!usable.length) blocked = excluded.length ? 'only an undeliverable address' : 'no email';
    return {
      key: `${index}:${sourceId || name || index}`,
      index,
      name,
      title: (row[columns.currentTitle] ?? '').trim(),
      company: (row[columns.currentCompany] ?? '').trim(),
      location: (row[columns.location] ?? '').trim(),
      primaryEmail: usable[0]?.email ?? '',
      primaryKind: usable[0]?.kind ?? '',
      verification: usable[0]?.verification ?? '',
      excludedEmails: excluded,
      blocked,
      sendable: !blocked,
      payload: blocked ? null : juiceboxRowToCandidate(row, columns),
    };
  });
}

// Every number here is a reason a row will or will not be mailed, so the page
// can say which -- "no email" and "we dropped a bad one" look identical from a
// bare importable count.
export function summarizeImport(importRows) {
  const total = importRows.length;
  const sendable = importRows.filter((r) => r.sendable).length;
  const undeliverableOnly = importRows.filter((r) => r.blocked === 'only an undeliverable address').length;
  const noEmail = importRows.filter((r) => r.blocked === 'no email').length;
  const unnamed = importRows.filter((r) => r.blocked === 'no name').length;
  const unidentified = importRows.filter((r) => r.blocked === 'nothing to identify them by').length;
  // Only counted among the sendable ones: an unmailable row is already off and
  // reporting it twice, once as blocked and once as a duplicate, reads as two
  // separate problems.
  const duplicates = importRows.filter((r) => r.sendable && r.duplicateOf).length;
  return { total, sendable, undeliverableOnly, noEmail, unnamed, unidentified, duplicates };
}

export function describeImport(summary) {
  const { total, sendable, undeliverableOnly, noEmail, unnamed, unidentified, duplicates } =
    summary;
  if (!total) return 'No rows in that file.';
  const parts = [`${total} ${total === 1 ? 'row' : 'rows'}`, `${sendable} with a usable email`];
  if (duplicates) parts.push(`${duplicates} already in contacts`);
  if (noEmail) parts.push(`${noEmail} with no email`);
  if (undeliverableOnly) {
    parts.push(`${undeliverableOnly} whose only address is undeliverable`);
  }
  if (unnamed) parts.push(`${unnamed} with no name`);
  if (unidentified) parts.push(`${unidentified} with nothing to identify them by`);
  return `${parts.join(', ')}.`;
}

// --------------------------------------------------------------------------
// already in contacts
// --------------------------------------------------------------------------

// `upsert_candidates` dedupes on (source, source_id), so re-uploading this same
// export merges rather than duplicates -- that is what `sourceIdFor` buys. What
// it cannot see is the same human under a different source: a Foray-sourced
// profile, a hand-added contact or an intake caller each carry a different key,
// and a row for them lands as a second record. So the page asks the server,
// which is the only side that can see the CRM.

// Matches the request schema's own ceiling. No real export row carries more
// than three or four addresses; the cap is there so a malformed cell cannot
// send an unbounded list.
const MAX_LOOKUP_EMAILS = 10;

export function knownLookupPayload(importRows) {
  return importRows
    .filter((row) => row.sendable && row.payload)
    .map((row) => {
      // Every address on the row, not just the ones we would mail: matching is
      // exact, so an undeliverable work address is a bad one to send to and a
      // perfectly good one to recognise somebody by.
      const emails = [];
      for (const address of [
        ...(row.payload.emails ?? []),
        ...(row.excludedEmails ?? []).map((entry) => entry.email),
      ]) {
        const clean = normalizeEmail(address);
        if (clean && !emails.includes(clean)) emails.push(clean);
      }
      return {
        key: row.key,
        emails: emails.slice(0, MAX_LOOKUP_EMAILS),
        linkedin_url: row.payload.linkedin_url ?? '',
        full_name: row.payload.full_name ?? '',
        company: row.payload.current_company ?? '',
      };
    })
    // A row with nothing to match on is not worth a round trip, and asking
    // about it would only invite a name-only guess.
    .filter(
      (person) =>
        person.emails.length || person.linkedin_url || (person.full_name && person.company),
    );
}

// Returns new rows rather than mutating: the table renders straight off them,
// and a stale answer arriving after a second file has to be droppable.
export function applyKnownMatches(importRows, matches) {
  const byKey = new Map((matches ?? []).map((match) => [match.key, match]));
  return importRows.map((row) => {
    const match = byKey.get(row.key);
    if (!match) return row.duplicateOf ? { ...row, duplicateOf: null } : row;
    return {
      ...row,
      duplicateOf: {
        candidateId: match.candidate_id,
        fullName: match.full_name,
        matchedOn: match.matched_on,
        reason: match.reason,
      },
    };
  });
}

// The selection with every known duplicate dropped. Used twice: once when the
// answer lands (so the default action is never the surprising one) and once
// when "Remove duplicates" is pressed.
export function withoutDuplicates(keys, importRows) {
  const duplicates = new Set(importRows.filter((row) => row.duplicateOf).map((row) => row.key));
  return [...(keys ?? [])].filter((key) => !duplicates.has(key));
}

// Chunked because /v1/candidates/bulk caps a request at 5000 candidates, and a
// 4MB export is well within a file a recruiter will actually drag in.
export const BULK_CHUNK = 500;

export function chunk(items, size = BULK_CHUNK) {
  const out = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

// --------------------------------------------------------------------------
// surviving a navigation
// --------------------------------------------------------------------------

// The review table lives in component state, so routing to Sequences and back
// used to unmount it and silently lose the file -- which reads as "the import
// screen wiped my work". The page keeps the raw text in sessionStorage and
// re-derives the rows on mount; re-parsing is instant and far smaller to store
// than the built rows.
export const IMPORT_STATE_KEY = 'juicebox.import.draft';

// Keys are index-prefixed, so a selection only means anything against the rows
// it was made on. Dropping unknown keys stops a restored selection from
// claiming rows that are not there.
export function pruneChosen(keys, importRows) {
  const valid = new Set(importRows.filter((r) => r.sendable).map((r) => r.key));
  return (keys ?? []).filter((key) => valid.has(key));
}

// Rejects anything that is not the shape we wrote -- a stale key from an older
// version of this page must not throw on mount.
export function readImportDraft(raw) {
  if (!raw) return null;
  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed.text !== 'string' || !parsed.text) return null;
  return {
    fileName: typeof parsed.fileName === 'string' ? parsed.fileName : '',
    text: parsed.text,
    chosen: Array.isArray(parsed.chosen) ? parsed.chosen.filter((k) => typeof k === 'string') : [],
    sheetNote: typeof parsed.sheetNote === 'string' ? parsed.sheetNote : '',
  };
}
