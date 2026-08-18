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
