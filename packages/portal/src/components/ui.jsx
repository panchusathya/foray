// The small shared pieces. Deliberately plain: the portal has one designed
// screen (the deck) and everything else should get out of its way.

export function Screen({ title, sub, action, children }) {
  return (
    <main className="screen">
      {(title || action) && (
        <header className="topbar">
          <div>
            {title && <h1>{title}</h1>}
            {sub && <div className="sub">{sub}</div>}
          </div>
          {action}
        </header>
      )}
      {children}
    </main>
  );
}

export function Empty({ title, children, action }) {
  return (
    <div className="empty">
      <h2>{title}</h2>
      {children && <p className="muted">{children}</p>}
      {action}
    </div>
  );
}

export function ErrorNote({ error }) {
  if (!error) return null;
  return <div className="banner error">{error.message || String(error)}</div>;
}

export function Skeleton({ height = 90, count = 3 }) {
  return (
    <div className="stack" aria-hidden="true">
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="skel" style={{ height }} />
      ))}
    </div>
  );
}

const STATUS_LABELS = {
  draft: 'Preparing',
  prepared: 'Ready to review',
  awaiting_consent: 'Awaiting your OK',
  submitting: 'Applying now',
  submitted: 'Sent',
  acknowledged: 'Received',
  interviewing: 'Interviewing',
  offer: 'Offer',
  rejected: 'Closed',
  withdrawn: 'Withdrawn',
};

export function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}

export function StatusChip({ status }) {
  return <span className={`status ${status}`}>{statusLabel(status)}</span>;
}
