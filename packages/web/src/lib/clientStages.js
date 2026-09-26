/**
 * Client dashboard vocabulary, shared by the client's own page and ours.
 *
 * STAGES mirrors models.CLIENT_STAGES, in funnel order: the piles render in
 * this order and the server refuses anything not on it.
 */

export const STAGES = [
  { id: 'in_process', label: 'In process', short: 'In process' },
  { id: 'first_round', label: 'First round', short: '1st round' },
  { id: 'second_round', label: 'Second round', short: '2nd round' },
  { id: 'offer', label: 'Offer', short: 'Offer' },
  { id: 'rejected', label: 'Rejected', short: 'Rejected' },
];

const BY_ID = Object.fromEntries(STAGES.map((s) => [s.id, s]));

export function stageLabel(id) {
  return BY_ID[id]?.label ?? id;
}

/**
 * The piles along the top: All, then one per stage. A pile with nobody in it
 * is not shown (visiblePiles) -- an empty "Offer" box is noise, and it comes
 * back the moment somebody lands there.
 */
export const PILES = [
  { id: 'all', label: 'All', stages: null },
  ...STAGES.map((s) => ({ id: s.id, label: s.label, stages: [s.id] })),
];

export function inPile(row, pileId) {
  const pile = PILES.find((p) => p.id === pileId);
  return !pile?.stages || pile.stages.includes(row.stage);
}

/** All, the active pile, and every pile that has somebody in it. */
export function visiblePiles(counts, activeId) {
  return PILES.filter((p) => p.id === 'all' || p.id === activeId || counts[p.id] > 0);
}

// Words people actually type in a tracking sheet, mapped onto a stage. Later
// rounds and "hired" fold into the nearest stage the client actually uses.
const ALIASES = [
  [/^(in process|in progress|awaiting|pending|submitted|shared|sent|new|in review|under review|awaiting review|to review|no response)$/, 'in_process'],
  [/^(1st|first|r1|round 1|first round|1st round|phone screen|screen|screening|intro)$/, 'first_round'],
  [/^(2nd|second|r2|round 2|second round|2nd round|technical|tech|3rd|third|third round|3rd round|final|final round|onsite|on site)$/, 'second_round'],
  [/^(offer|offered|offer extended|offer made|hired|placed|accepted|offer accepted|signed)$/, 'offer'],
  [/^(rejected|reject|declined|no|not a fit|pass|passed|no hire|rejected by client|withdrawn|withdrew|dropped|dropped out|candidate withdrew|not interested)$/, 'rejected'],
];

/** A stage id for free text, or '' when it names no stage we know. */
export function parseStage(text) {
  const folded = String(text ?? '')
    .toLowerCase()
    .replace(/[_\-]+/g, ' ')
    .replace(/[^a-z0-9 ]+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!folded) return '';
  if (BY_ID[folded.replace(/ /g, '_')]) return folded.replace(/ /g, '_');
  for (const [pattern, stage] of ALIASES) if (pattern.test(folded)) return stage;
  return '';
}

const LINKEDIN = /(?:https?:\/\/)?(?:[a-z]{2,3}\.)?linkedin\.com\/in\/[^\s,\t"'<>]+/i;

/**
 * Parse pasted rows -- straight from a spreadsheet (tabs), a CSV, or one
 * LinkedIn URL per line -- into share entries.
 *
 * Per line: the first LinkedIn URL is the identity, the first cell that reads
 * as a stage is the stage, and the first remaining cell is the name. A header
 * row ("Name, LinkedIn, Status") yields no URL and no real name, so it is
 * recognised and dropped rather than shared as a person called "Name".
 */
export function parsePastedRows(text) {
  const rows = [];
  const lines = String(text ?? '').split(/\r?\n/);
  lines.forEach((raw, index) => {
    const line = raw.trim();
    if (!line) return;
    const cells = (line.includes('\t') ? line.split('\t') : line.split(','))
      .map((c) => c.trim().replace(/^"|"$/g, '').trim())
      .filter(Boolean);
    let linkedin = '';
    let urlBeforeRest = false;
    const rest = [];
    for (const cell of cells) {
      const url = cell.match(LINKEDIN);
      if (!linkedin && url) {
        linkedin = url[0].startsWith('http') ? url[0] : `https://${url[0]}`;
        urlBeforeRest = rest.length === 0;
        continue;
      }
      rest.push(cell);
    }
    // The first cell is the name even when it happens to read as a stage
    // ("Pass", "First"): a stage is only taken from a later cell, or from a
    // lone cell *after* a URL ("linkedin.com/in/x, rejected"). Before the URL
    // it is a name column.
    let name = rest[0] ?? '';
    let stage = '';
    if (linkedin && urlBeforeRest && rest.length === 1 && parseStage(rest[0])) {
      stage = parseStage(rest[0]);
      name = '';
    } else {
      stage = rest.slice(1).map(parseStage).find(Boolean) ?? '';
    }
    if (!linkedin && /^(name|full name|candidate|person)$/i.test(name)) return; // header
    if (!linkedin && !name) return;
    rows.push({ line: index + 1, full_name: name, linkedin_url: linkedin, stage, raw: line });
  });
  return rows;
}

function csvCell(value) {
  const text = String(value ?? '');
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** The grid as CSV, for whoever still wants it in a spreadsheet. */
export function toCsv(rows) {
  const header = [
    'Name',
    'Title',
    'Company',
    'Location',
    'LinkedIn',
    'Role',
    'Stage',
    'Stage changed',
    'Changed by',
    'Shared',
    'Latest note',
  ];
  const lines = rows.map((r) =>
    [
      r.full_name,
      r.current_title,
      r.current_company,
      r.location,
      r.linkedin_url,
      r.role_title,
      stageLabel(r.stage),
      r.stage_changed_at?.slice(0, 10),
      r.stage_changed_by_name,
      r.shared_at?.slice(0, 10),
      r.latest_note?.body,
    ]
      .map(csvCell)
      .join(','),
  );
  return [header.join(','), ...lines].join('\n');
}

export function downloadCsv(filename, rows) {
  const blob = new Blob([toCsv(rows)], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/** "3h ago" style, for the Updated column. */
export function relativeTime(iso, now = Date.now()) {
  if (!iso) return '';
  const seconds = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
