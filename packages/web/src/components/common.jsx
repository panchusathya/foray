/** Small shared presentational pieces. */

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

export function Stat({ value, label }) {
  return (
    <div className="card stat">
      <div className="value">{value}</div>
      <div className="label">{label}</div>
    </div>
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
