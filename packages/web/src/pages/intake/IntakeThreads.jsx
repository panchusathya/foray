import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../../components/common.jsx';
import { ReviewReason } from '../../components/ReviewReason.jsx';
import { intakeApi, messageReceipt } from '../../lib/intake.js';
import { useAsync } from '../../lib/useAsync.js';

function when(value) {
  return value ? new Date(value).toLocaleString() : '—';
}

function ThreadMessages({ threadId }) {
  const detail = useAsync(() => intakeApi.getThread(threadId), [threadId]);
  if (detail.loading) return <Loading label="Loading messages…" />;
  if (detail.error) return <ErrorBox error={detail.error} />;
  const messages = detail.data?.messages ?? [];
  if (!messages.length) {
    return <div className="small muted">No messages in this thread yet.</div>;
  }
  return (
    <div className="stack" style={{ gap: 6 }} data-testid="intake-thread-messages">
      {messages.map((message) => {
        const inbound = message.direction === 'inbound';
        return (
          <div key={message.id} className="card stack" style={{ gap: 4 }} data-testid="intake-text-message">
            <div className="row small spread">
              <span className="row" style={{ gap: 6 }}>
                <Badge tone={inbound ? 'accent' : 'strong'}>
                  {inbound ? '← candidate' : '→ agent'}
                </Badge>
                {message.asked_field ? (
                  <span className="muted">asked {message.asked_field}</span>
                ) : null}
                {!inbound && messageReceipt(message) ? (
                  <span className="muted">{messageReceipt(message)}</span>
                ) : null}
              </span>
              <span className="muted">{when(message.sent_at || message.created_at)}</span>
            </div>
            <pre className="message-body">{message.body || '—'}</pre>
          </div>
        );
      })}
    </div>
  );
}

function ThreadCard({ row }) {
  const [open, setOpen] = useState(false);
  const who = row.contact_name || row.contact_email || row.from_address || 'unknown';
  return (
    <div className="card stack" data-testid="intake-thread-row">
      <button type="button" className="option" onClick={() => setOpen((value) => !value)}>
        <span className="thread-head">
          <span className="thread-subject">
            {open ? '▾' : '▸'} {who}
          </span>
          <span className="thread-meta muted small">
            {row.inbound_count} in / {row.outbound_count} out ·{' '}
            {when(row.last_inbound_at || row.created_at)}
          </span>
        </span>
      </button>
      <div className="row spread">
        <div className="row" style={{ gap: 8 }}>
          <Badge>{row.channel}</Badge>
          <Badge tone={row.review_needed ? 'partial' : ''}>{row.status}</Badge>
          {row.required_complete ? null : <span className="small muted">gate open</span>}
        </div>
        {row.candidate_id ? (
          <Link to={`/contacts/${row.candidate_id}`}>Open record</Link>
        ) : (
          <span className="muted small">unattributed</span>
        )}
      </div>
      {row.review_needed ? (
        <ReviewReason reason={row.review_reason} />
      ) : null}
      {open ? <ThreadMessages threadId={row.id} /> : null}
    </div>
  );
}

export function IntakeThreads() {
  const threads = useAsync(() => intakeApi.listThreads({}), []);
  const rows = threads.data ?? [];

  return (
    <div className="stack" data-testid="intake-threads">
      {threads.loading ? <Loading label="Loading text conversations…" /> : null}
      <ErrorBox error={threads.error} />
      {!threads.loading && !rows.length ? (
        <Empty>No text intake conversations yet.</Empty>
      ) : (
        <div className="stack" style={{ gap: 8 }}>
          {rows.map((row) => (
            <ThreadCard key={row.id} row={row} />
          ))}
        </div>
      )}
    </div>
  );
}
