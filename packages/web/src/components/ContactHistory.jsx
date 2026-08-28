/**
 * Per-candidate contact history: the stats strip plus the actual emails,
 * grouped into conversation threads.
 *
 * Bodies render as plain text only. Synced mail can be HTML, and there is no
 * sanitizer dependency in this repo — so rather than injecting body_html, a
 * link to the provider's own web client hands off for the rich version.
 */

import { useEffect, useState } from 'react';

import { api, providerLabel } from '../lib/api.js';
import { messageReceipt } from '../lib/intake.js';
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
          <Badge>
            {message.source === 'sequence' ? 'sequence' : providerLabel(message.source)}
          </Badge>
          {message.status === 'failed' ? <Badge tone="partial">failed</Badge> : null}
          {message.attachment_count ? <span className="muted">📎</span> : null}
        </span>
        <span className="muted small">{when(message.sent_at || message.created_at)}</span>
      </div>
      <div className="small muted wrap-anywhere">
        {inbound ? `from ${message.from_address}` : `to ${message.to_address}`}
        {message.reply_intent ? ` · ${message.reply_intent}` : ''}
      </div>
      <pre className="message-body">{textOf(message)}</pre>
      {message.web_link ? (
        <div className="small">
          <a href={message.web_link} target="_blank" rel="noreferrer">
            Open in {providerLabel(message.source)} →
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
        <span className="thread-head">
          {/* Truncates rather than pushing the count and date off the page:
              a flex item will not shrink below its content without min-width:0,
              and real subjects are longer than the column. */}
          <span className="thread-subject" title={thread.subject}>
            {open ? '▾' : '▸'} {thread.subject}
          </span>
          <span className="thread-meta muted small">
            {thread.messages.length} email{thread.messages.length === 1 ? '' : 's'} ·{' '}
            {shortDate(thread.last_at)}
          </span>
        </span>
      </button>
      {open ? thread.messages.map((message) => <MessageRow key={message.id} message={message} />) : null}
    </div>
  );
}

export function ContactHistory({ candidateId, onCount }) {
  const timeline = useAsync(() => api.candidateTimeline(candidateId), [candidateId]);
  const total =
    (timeline.data?.stats?.outbound_count ?? 0) + (timeline.data?.stats?.inbound_count ?? 0);

  // Reported up so the tab strip can label itself without a second request.
  // Above the early returns: a hook cannot sit behind a conditional.
  useEffect(() => {
    if (timeline.data) onCount?.(total);
  }, [onCount, timeline.data, total]);

  if (timeline.loading) return <Loading label="Loading contact history…" />;
  if (timeline.error) return <ErrorBox error={timeline.error} />;

  const {
    stats,
    threads,
    events = [],
    intake_threads: intakeThreads = [],
    openinstinct_conversations: openInstinctConversations = [],
    openinstinct_available: openInstinctAvailable = false,
    openinstinct_application_tasks: openInstinctApplicationTasks = [],
  } = timeline.data;
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

      {events.length ? (
        <div className="stack" style={{ gap: 6 }} data-testid="intake-events">
          {events.map((event) => (
            <div key={event.id} className="card row spread small">
              <span>{event.type.replace('intake.', '').replaceAll('_', ' ')}</span>
              <span className="muted">{when(event.created_at)}</span>
            </div>
          ))}
        </div>
      ) : null}

      {intakeThreads.length ? (
        <div className="stack" style={{ gap: 8 }} data-testid="intake-threads">
          {intakeThreads.map((thread) => (
            <IntakeThreadHistory key={thread.thread_id} thread={thread} />
          ))}
        </div>
      ) : null}

      {openInstinctConversations.length ? (
        <div className="stack" style={{ gap: 8 }} data-testid="openinstinct-conversations">
          {openInstinctConversations.map((conversation, index) => (
            <OpenInstinctConversation
              key={conversation.id || conversation.url || index}
              conversation={conversation}
            />
          ))}
        </div>
      ) : openInstinctAvailable ? (
        <div className="card empty">No OpenInstinct conversations yet.</div>
      ) : null}

      {openInstinctApplicationTasks.length ? (
        <div className="stack" style={{ gap: 6 }} data-testid="openinstinct-application-tasks">
          {openInstinctApplicationTasks.map((task) => (
            <div key={task.id} className="card row spread small">
              <span>OpenInstinct application · {String(task.status || '').replaceAll('_', ' ')}</span>
              <span className="muted">{when(task.reported_at || task.updated_at)}</span>
            </div>
          ))}
        </div>
      ) : null}

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

function OpenInstinctConversation({ conversation }) {
  const [open, setOpen] = useState(true);
  const messages = Array.isArray(conversation.messages) ? conversation.messages : [];
  return (
    <div className="stack" style={{ gap: 6 }} data-testid="openinstinct-conversation">
      <button type="button" className="option" onClick={() => setOpen((value) => !value)}>
        <span className="thread-head">
          <span className="thread-subject">
            {open ? '▾' : '▸'} OpenInstinct · {conversation.channel || 'chat'}
          </span>
          <span className="thread-meta muted small">{shortDate(conversation.updated_at || conversation.last_at)}</span>
        </span>
      </button>
      {open
        ? messages.map((message, index) => (
            <div key={message.id || index} className="card stack" style={{ gap: 6 }}>
              <div className="row small spread">
                <Badge tone={message.direction === 'inbound' ? 'accent' : 'strong'}>
                  {message.direction === 'inbound' ? '← candidate' : '→ OpenInstinct'}
                </Badge>
                <span className="muted small">{when(message.created_at)}</span>
              </div>
              <pre className="message-body">{message.body || '—'}</pre>
            </div>
          ))
        : null}
      {conversation.url ? (
        <a className="small" href={conversation.url} target="_blank" rel="noreferrer">
          Open conversation →
        </a>
      ) : null}
    </div>
  );
}

function IntakeThreadHistory({ thread }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="stack" style={{ gap: 6 }} data-testid="intake-thread-history">
      <button type="button" className="option" onClick={() => setOpen((value) => !value)}>
        <span className="thread-head">
          <span className="thread-subject">
            {open ? '▾' : '▸'} Intake · {thread.channel}
          </span>
          <span className="thread-meta muted small">
            {thread.messages.length} message{thread.messages.length === 1 ? '' : 's'} ·{' '}
            {shortDate(thread.last_at)}
          </span>
        </span>
      </button>
      {open
        ? thread.messages.map((message) => {
            const inbound = message.direction === 'inbound';
            return (
              <div
                key={message.id}
                className="card stack"
                style={{ gap: 6 }}
                data-testid="intake-history-message"
              >
                <div className="row small spread">
                  <Badge tone={inbound ? 'accent' : 'strong'}>
                    {inbound ? '← candidate' : '→ agent'}
                  </Badge>
                  <span className="muted small">
                    {!inbound && messageReceipt(message) ? `${messageReceipt(message)} · ` : ''}
                    {when(message.sent_at || message.created_at)}
                  </span>
                </div>
                <pre className="message-body">{message.body || '—'}</pre>
              </div>
            );
          })
        : null}
    </div>
  );
}
