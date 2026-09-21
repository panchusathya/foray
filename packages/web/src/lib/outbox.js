/**
 * Re-enrol helpers for the Outbox page.
 *
 * Pure on purpose: the arithmetic that decides *when a person gets emailed*
 * is the part worth testing without a browser, and the part that goes wrong
 * quietly (a datetime-local value is in the browser's zone; the API wants UTC).
 */

/** The three answers to "and when should it go instead?", default first. */
export const REENROL_WHEN = [
  {
    id: 'next_slot',
    label: "At the step's usual send time",
    hint: 'The next time its hour comes round. The wait before this step is not counted again.',
  },
  {
    id: 'now',
    label: 'Now',
    hint: 'Due immediately. Sends straight away if the sequence is live.',
  },
  {
    id: 'at',
    label: 'At a time I pick',
    hint: '',
  },
];

function pad(value) {
  return String(value).padStart(2, '0');
}

/** A Date as the `YYYY-MM-DDTHH:mm` string an <input type="datetime-local"> wants. */
export function toLocalInput(date) {
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/**
 * A datetime-local value, read in the browser's zone, as the UTC ISO string
 * the API stores. Empty or unparseable gives null rather than an Invalid Date
 * that JSON.stringify would silently turn into null anyway -- better to know.
 */
export function localToIso(local) {
  if (!local) return null;
  const date = new Date(local);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

/** Tomorrow at 09:00 local: the usual answer to "not now -- then when?". */
export function defaultPickTime(now = new Date()) {
  const date = new Date(now);
  date.setDate(date.getDate() + 1);
  date.setHours(9, 0, 0, 0);
  return toLocalInput(date);
}

/** The request body for /in-flight/reenroll. Throws when "at" has no time. */
export function reenrolPayload({ ids, when, at, force = false }) {
  const payload = { enrollment_ids: ids, when, force };
  if (when === 'at') {
    const iso = localToIso(at);
    if (!iso) throw new Error('Pick a date and time first.');
    payload.send_at = iso;
  }
  return payload;
}

function defaultFormat(iso) {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

/**
 * The one-line outcome shown after a re-enrol. Reports every population the
 * API distinguishes, because the interesting ones (someone replied meanwhile;
 * a row had nothing left to send) are exactly the ones a count would hide.
 */
export function describeReenrol(result, when, format = defaultFormat) {
  const bits = [];
  if (result.resumed) {
    let tail = '.';
    if (when === 'now') {
      tail =
        result.sent === result.resumed
          ? ` — ${result.sent} sent.`
          : ` — ${result.sent} sent, ${result.resumed - result.sent} due now and waiting on the sender.`;
    } else if (result.next_send_at) {
      tail = ` — next email ${format(result.next_send_at)}.`;
    }
    bits.push(`Re-enrolled ${result.resumed}${tail}`);
  }
  if (result.completed) {
    bits.push(
      `${result.completed} had nothing left to send and ${result.completed === 1 ? 'is' : 'are'} marked complete.`,
    );
  }
  const skipped = result.skipped ?? [];
  if (skipped.length) {
    const names = skipped.map((row) => row.full_name || 'someone').join(', ');
    bits.push(
      `${names} replied to you in the last ${skipped[0].quiet_days} days and ${skipped.length === 1 ? 'was' : 'were'} left stopped.`,
    );
  }
  if (result.not_found) {
    bits.push(`${result.not_found} could not be re-enrolled.`);
  }
  return bits.join(' ') || 'Nothing changed.';
}
