/**
 * One way of writing a phone number, everywhere it is shown.
 *
 * The API stores E.164 (`intake.phones.lookup_key` refuses anything it cannot
 * normalise), which is correct and unambiguous and reads badly: `+14155550123`
 * is a string a person has to decode before they can dial it. Every surface
 * that showed a number was doing its own thing about that, or nothing at all.
 *
 * Only North American numbers get the grouped form. A country code is between
 * one and three digits and the national grouping differs by country, so a
 * general "insert spaces every three" reformats a London number into something
 * that looks wrong to anybody who knows it. For everything else the stored
 * E.164 *is* the presentation: unfamiliar, but never misleading.
 */

const NANP = /^\+1(\d{3})(\d{3})(\d{4})$/;

/** `+14155550123` -> `+1 (415) 555-0123`. Anything else is returned unchanged. */
export function formatPhone(value) {
  const raw = (value ?? '').trim();
  if (!raw) return '';
  const match = NANP.exec(raw);
  if (!match) return raw;
  const [, area, prefix, line] = match;
  return `+1 (${area}) ${prefix}-${line}`;
}

/** The number to show for a row, formatted, or `''` when there is none. */
export function primaryPhone(row) {
  const phones = row?.phones ?? [];
  return formatPhone(phones[0] ?? '');
}

/** Every number a row has, formatted, for a tooltip. */
export function allPhones(row) {
  return (row?.phones ?? []).map(formatPhone).join(', ');
}

/** A `tel:` href. Always the raw E.164 — a dialler wants digits, not spaces. */
export function telHref(value) {
  const raw = (value ?? '').trim();
  return raw ? `tel:${raw.replace(/[^\d+]/g, '')}` : '';
}
