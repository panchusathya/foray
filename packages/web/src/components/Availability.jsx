/**
 * Whether this person is on the market, and how fresh that answer is.
 *
 * "Not asked" is rendered as its own muted state rather than folded in with
 * "not looking". The two mean opposite things to a recruiter deciding who to
 * call, and the database is mostly the former.
 */
// The same four tones the criteria list uses, so a green dot means the same
// thing everywhere on the page.
const TONE = {
  active: 'strong',
  open: 'strong',
  passive: 'partial',
  not_looking: 'miss',
  unknown: 'unknown',
};

function money(amount, currency) {
  if (amount == null) return '';
  const code = currency && currency !== 'UNKNOWN' ? currency : '';
  const rounded = amount >= 1000 ? `${Math.round(amount / 1000)}k` : `${Math.round(amount)}`;
  return `${code ? `${code} ` : ''}${rounded}`;
}

export function AvailabilityBadge({ availability }) {
  if (!availability) return null;
  const tone = TONE[availability.state] ?? 'muted';
  return (
    <span className={`avail avail-${tone}`} title={availability.detail || undefined}>
      <span className="avail-dot" aria-hidden="true" />
      {availability.label}
      {availability.stale ? <span className="avail-stale"> · stale</span> : null}
    </span>
  );
}

/** The line under the badge: timeline, comp ask, competing processes. */
export function AvailabilityDetail({ availability }) {
  if (!availability || availability.state === 'unknown') return null;

  const ask = money(availability.target_base ?? availability.target_total, availability.comp_currency);
  const bits = [availability.detail, ask ? `asking ${ask}` : ''].filter(Boolean);
  if (availability.willing_to_relocate === true) bits.push('will relocate');
  if (availability.work_authorization) bits.push(availability.work_authorization);

  if (!bits.length) return null;
  return (
    <p className="small muted avail-detail">
      {bits.join(' · ')}
      {availability.confirmed ? '' : ' · unconfirmed'}
    </p>
  );
}
