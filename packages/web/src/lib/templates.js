/**
 * Finding placeholders the renderer will not substitute.
 *
 * The template language accepts `{{ first_name }}` and `{{ first_name | there }}`
 * for variables and `{{ai: ...}}` for a directive, and nothing else. A name with
 * a space in it — `{{First name}}` — matches neither, so `render()` never sees a
 * placeholder there at all and the raw braces survive into the sent email. That
 * is not hypothetical: it went out.
 *
 * Mirrors `malformed_placeholders()` in outreach/templates.py, deliberately, the
 * way schedulePreview() mirrors schedule_step_at(). The server is still the one
 * that refuses to send; this exists so the recruiter finds out while the step is
 * in front of them rather than from a failed send an hour later.
 */

// Kept character-for-character in step with _VAR_RE and _AI_RE on the server.
const VAR_RE = /\{\{\s*([a-zA-Z0-9_.]+)\s*(?:\|\s*([^}]*?)\s*)?\}\}/g;
const AI_RE = /\{\{\s*ai\s*:\s*([^{}]+?)\s*\}\}/gi;

function claimedStarts(template) {
  const starts = new Set();
  for (const re of [VAR_RE, AI_RE]) {
    re.lastIndex = 0;
    let match = re.exec(template);
    while (match) {
      starts.add(match.index);
      match = re.exec(template);
    }
  }
  return starts;
}

function span(template, start, limit = 60) {
  const end = template.indexOf('}}', start);
  if (end !== -1 && end + 2 - start <= limit) return template.slice(start, end + 2);
  // Unterminated, or too long to quote whole: show the rest of its line, and
  // ellipsise only when there was more to show.
  const rest = template.slice(start).split('\n')[0];
  return rest.length <= limit ? rest.trimEnd() : `${rest.slice(0, limit).trimEnd()}…`;
}

/**
 * Every `{{` in `template` that starts neither a variable nor a directive,
 * verbatim, deduped, in first-seen order.
 *
 * Scanning for the opening braces rather than for whole `{{...}}` spans is the
 * point: an unterminated `{{ first_name` has no closing braces to match on and
 * ships just as literally as a misspelt one.
 */
export function malformedPlaceholders(template) {
  if (!template) return [];
  const claimed = claimedStarts(template);
  const found = [];
  const open = /\{\{/g;
  let match = open.exec(template);
  while (match) {
    if (!claimed.has(match.index)) {
      const text = span(template, match.index);
      if (!found.includes(text)) found.push(text);
    }
    match = open.exec(template);
  }
  return found;
}

/** The same, across a step's subject and body. */
export function malformedInStep(step) {
  const found = [
    ...malformedPlaceholders(step?.subject_template ?? ''),
    ...malformedPlaceholders(step?.body_template ?? ''),
  ];
  return [...new Set(found)];
}
