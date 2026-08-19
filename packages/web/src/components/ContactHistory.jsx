/**
 * Per-candidate contact history: the stats strip plus the actual emails,
 * grouped into conversation threads.
 *
 * Bodies render as plain text only. Synced Outlook mail can be HTML, and
 * there is no sanitizer dependency in this repo — so rather than injecting
 * body_html, an "Open in Outlook" link (Graph's own webLink) hands off to
 * the real client for the rich version.
 */

import { useState } from 'react';

import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';
import { Badge, Empty, ErrorBox, Loading } from './common.jsx';

function when(value) {
  return value ? new Date(value).toLocaleString() : '—';
}

function shortDate(value) {
  return value ? new Date(value).toLocaleDateString() : '—';
}

// An HTML body stored without a plaintext twin still needs something legible.
function textOf(message) {
  if (message.body) return message.body;
  return '(HTML email — open it in Outlook for the formatted version)';
}

function MessageRow({ message }) {
  const inbound = message.direction === 'inbound';
  return (
    <div className="card stack" style={{ gap: 6 }} data-testid="history-message">
      <div className="row small spread">
        <span className="row" style={{ gap: 6 }}>
          <Badge tone={inbound ? 'accent' : 'strong'}>{inbound ? '← reply' : '→ sent'}</Badge>
          <Badge>{message.source === 'outlook' ? 'Outlook' : 'sequence'}</Badge>
          {message.status === 'failed' ? <Badge tone="partial">failed</Badge> : null}
          {message.attachment_count ? <span className="muted">📎</span> : null}
        </span>
        <span className="muted small">{when(message.sent_at || message.created_at)}</span>
      </div>
      <div className="small muted">
        {inbound ? `from ${message.from_address}` : `to ${message.to_address}`}
        {message.reply_intent ? ` · ${message.reply_intent}` : ''}
      </div>
      <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'inherit' }}>
        {textOf(message)}
      </pre>
      {message.web_link ? (
        <div className="small">
          <a href={message.web_link} target="_blank" rel="noreferrer">
            Open in Outlook →
          </a>
        </div>
      ) : null}
    </div>
  );
}

function Thread({ thread, defaultOpen }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="stack" style={{ gap: 6 }} data-testid="history-thread">
      <button type="button" className="option" onClick={() => setOpen((value) => !value)}>
        <span className="spread" style={{ width: '100%' }}>
          <span>
            {open ? '▾' : '▸'} {thread.subject}
          </span>
          <span className="muted small">
            {thread.messages.length} email{thread.messages.length === 1 ? '' : 's'} ·{' '}
            {shortDate(thread.last_at)}
          </span>
        </span>
      </button>
      {open ? thread.messages.map((message) => <MessageRow key={message.id} message={message} />) : null}
    </div>
  );
}

export function ContactHistory({ candidateId }) {
  const timeline = useAsync(() => api.candidateTimeline(candidateId), [candidateId]);

  if (timeline.loading) return <Loading label="Loading contact history…" />;
  if (timeline.error) return <ErrorBox error={timeline.error} />;

  const { stats, threads } = timeline.data;
  return (
    <div className="stack" data-testid="contact-history">
      <div className="grid four">
        <div className="card stat">
          <div className="value">{shortDate(stats.first_contacted_at)}</div>
          <div className="label">first contacted</div>
        </div>
        <div className="card stat">
          <div className="value">{shortDate(stats.last_emailed_at)}</div>
          <div className="label">last email sent</div>
        </div>
        <div className="card stat">
          <div className="value">{shortDate(stats.last_reply_at)}</div>
          <div className="label">last reply</div>
        </div>
        <div className="card stat">
          <div className="value">
            {stats.outbound_count} / {stats.inbound_count}
          </div>
          <div className="label">sent / replies</div>
        </div>
      </div>

      {threads.length ? (
        threads.map((thread, index) => (
          <Thread
            key={thread.conversation_id || index}
            thread={thread}
            defaultOpen={index === 0}
          />
        ))
      ) : (
        <Empty>
          No emails with this candidate yet. Sequence sends land here automatically;
          connect Outlook thread sync in Settings to also see mail you write by hand.
        </Empty>
      )}
    </div>
  );
}
