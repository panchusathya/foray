/** Small shared presentational pieces. */

import { Link } from 'react-router-dom';

/**
 * The Foray logo mark — a 2×2 grid of squares, three purple and one gold,
 * lifted from the goforay.io landing. Decorative; the wordmark beside it
 * carries the name.
 */
export function BrandMark() {
  return (
    <span className="brand-glyph" aria-hidden="true">
      <i />
      <i />
      <i />
      <i />
    </span>
  );
}

export function PageHeader({ title, subtitle, actions }) {
  return (
    <div className="page-header spread">
      <div>
        <h1>{title}</h1>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
      {actions ? <div className="row">{actions}</div> : null}
    </div>
  );
}

/**
 * Initials in a circle.
 *
 * Decorative only -- `aria-hidden`, because the name it stands for is always
 * rendered right beside it and a screen reader announcing "AC" twice is worse
 * than silence.
 */
export function Avatar({ name, size = 26 }) {
  const parts = (name || '')
    .replace(/[^\p{L}\p{N} ]/gu, ' ')
    .split(' ')
    .filter(Boolean);
  const label = parts.length
    ? (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase()
    : '?';
  return (
    <span
      className="avatar"
      aria-hidden="true"
      style={{ width: size, height: size, fontSize: Math.round(size * 0.38) }}
    >
      {label}
    </span>
  );
}

export function ErrorBox({ error }) {
  if (!error) return null;
  return <div className="error">{error.message ?? String(error)}</div>;
}

export function Empty({ children }) {
  return <div className="card empty">{children}</div>;
}

export function Loading({ label = 'Loading…' }) {
  return <div className="card empty">{label}</div>;
}

export function Stat({ value, label, note }) {
  return (
    <div className="card stat">
      <div className="value">{value}</div>
      <div className="label">{label}</div>
      {note ? <div className="note">{note}</div> : null}
    </div>
  );
}

/**
 * A Stat that opens the list it counts.
 *
 * Same markup as Stat so the two never drift apart visually -- a number you
 * can click and one you cannot should not look like different components,
 * they should look like the same tile with a cursor.
 */
export function StatLink({ value, label, to, note }) {
  return (
    <Link className="card stat is-link" to={to} data-testid="stat-link">
      <div className="value">{value}</div>
      <div className="label">{label}</div>
      {note ? <div className="note">{note}</div> : null}
    </Link>
  );
}

export function Badge({ tone = '', children }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function ScoreBar({ value }) {
  const percent = Math.max(0, Math.min(1, value ?? 0)) * 100;
  return (
    <div className="bar">
      <div style={{ width: `${percent}%` }} />
    </div>
  );
}

/**
 * The preset-view tab strip.
 *
 * Each tab is a plain link that drops every other param on purpose: search,
 * sort and page all belong to the view you are leaving. `carry` names the
 * exceptions — params that describe *which population* you are looking at
 * rather than how you are looking at it, and which would be maddening to
 * re-pick on every tab.
 */
export function ViewTabs({ views, activeId, basePath, carry = {} }) {
  const kept = Object.entries(carry)
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => `&${key}=${encodeURIComponent(value)}`)
    .join('');
  return (
    <nav className="viewtabs" data-testid="view-tabs">
      {views.map((view) => (
        <Link
          key={view.id}
          to={`${basePath}?view=${view.id}${kept}`}
          className={view.id === activeId ? 'active' : ''}
        >
          {view.label}
        </Link>
      ))}
    </nav>
  );
}
