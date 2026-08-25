import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../../components/common.jsx';
import { ReviewReason } from '../../components/ReviewReason.jsx';
import { api } from '../../lib/api.js';
import { formatComp, formatTimeline, intakeApi, messageReceipt } from '../../lib/intake.js';
import { formatPhone } from '../../lib/phone.js';
import { useAsync } from '../../lib/useAsync.js';

function when(value) {
  return value ? new Date(value).toLocaleString() : '—';
}

function AttributeForm({ row, attribute, onDone }) {
  const [query, setQuery] = useState('');
  const [picked, setPicked] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const people = useAsync(
    () => (query.length >= 2 ? api.listCandidates({ q: query, limit: 8 }) : Promise.resolve([])),
    [query],
  );

  async function submit(event) {
    event.preventDefault();
    if (!picked) return;
    setSaving(true);
    setError(null);
    try {
      await attribute(row.id, { candidate_id: picked });
      onDone();
    } catch (caught) {
      setError(caught);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="stack" style={{ gap: 6 }} onSubmit={submit}>
      <ErrorBox error={error} />
      <input
        type="search"
        placeholder="Search a name to attribute"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {(people.data ?? []).map((person) => (
        <label key={person.id} className="row" style={{ gap: 8 }}>
          <input
            type="radio"
            name={`attr-${row.id}`}
            checked={picked === person.id}
            onChange={() => setPicked(person.id)}
          />
          <span>
            {person.full_name}
            <span className="muted small"> {person.current_title}</span>
          </span>
        </label>
      ))}
      <button type="submit" className="tiny" disabled={!picked || saving}>
        Attribute
      </button>
    </form>
  );
}

function CallReviewBody({ callId }) {
  const detail = useAsync(() => intakeApi.getCall(callId), [callId]);
  if (detail.loading) return <Loading label="Loading transcript…" />;
  if (detail.error) return <ErrorBox error={detail.error} />;
  const call = detail.data;
  const extracted = call.extracted || {};
  const titles = ((extracted.spec || {}).job_titles || []).join(', ');
  return (
    <div className="stack" style={{ gap: 8 }} data-testid="intake-review-call-detail">
      <div className="grid three">
        <div>
          <div className="small muted">Timeline</div>
          <div>{formatTimeline(extracted.timeline)}</div>
        </div>
        <div>
          <div className="small muted">Target comp</div>
          <div>{formatComp(extracted.target_total || extracted.target_base)}</div>
        </div>
        <div>
          <div className="small muted">Work auth</div>
          <div>{extracted.work_authorization || '—'}</div>
        </div>
      </div>
      {titles ? <div className="small muted">Roles: {titles}</div> : null}
      {call.transcript?.trim() ? (
        <pre className="message-body">{call.transcript}</pre>
      ) : (
        <div className="small muted">No transcript captured yet.</div>
      )}
    </div>
  );
}

function ThreadReviewBody({ threadId }) {
  const detail = useAsync(() => intakeApi.getThread(threadId), [threadId]);
  if (detail.loading) return <Loading label="Loading messages…" />;
  if (detail.error) return <ErrorBox error={detail.error} />;
  const messages = detail.data?.messages ?? [];
  if (!messages.length) {
    return <div className="small muted">No messages in this thread yet.</div>;
  }
  return (
    <div className="stack" style={{ gap: 6 }} data-testid="intake-review-thread-detail">
      {messages.map((message) => {
        const inbound = message.direction === 'inbound';
        return (
          <div key={message.id} className="card stack" style={{ gap: 4 }}>
            <div className="row small spread">
              <Badge tone={inbound ? 'accent' : 'strong'}>
                {inbound ? '← candidate' : '→ agent'}
              </Badge>
              <span className="muted">
                {!inbound && messageReceipt(message) ? `${messageReceipt(message)} · ` : ''}
                {when(message.sent_at || message.created_at)}
              </span>
            </div>
            <pre className="message-body">{message.body || '—'}</pre>
          </div>
        );
      })}
    </div>
  );
}

function ThreadRow({ row, onDone }) {
  const [open, setOpen] = useState(true);
  const who = row.contact_name || row.contact_email || formatPhone(row.from_address);
  return (
    <div className="card stack" data-testid="intake-review-thread">
      <button type="button" className="option" onClick={() => setOpen((value) => !value)}>
        <span className="thread-head">
          <span className="thread-subject">
            {open ? '▾' : '▸'} Text · {row.channel}
          </span>
          <span className="thread-meta muted small">
            {when(row.last_inbound_at || row.created_at)}
          </span>
        </span>
      </button>
      <div className="row spread">
        <div>
          <ReviewReason reason={row.review_reason} />
          <div className="small muted">
            {who || 'no contact details'} · {row.inbound_count} in / {row.outbound_count} out
          </div>
        </div>
        {row.candidate_id ? (
          <Link to={`/contacts/${row.candidate_id}`}>Open record</Link>
        ) : (
          <span className="muted small">unattributed</span>
        )}
      </div>
      {open ? <ThreadReviewBody threadId={row.id} /> : null}
      {!row.candidate_id ? (
        <AttributeForm row={row} attribute={intakeApi.patchThread} onDone={onDone} />
      ) : null}
    </div>
  );
}

function CallRow({ row, onDone }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="card stack" data-testid="intake-review-row">
      <button type="button" className="option" onClick={() => setOpen((value) => !value)}>
        <span className="thread-head">
          <span className="thread-subject">
            {open ? '▾' : '▸'} Call ·{' '}
            {row.contact_name || row.contact_email || formatPhone(row.from_number) || 'unknown'}
          </span>
          <span className="thread-meta muted small">
            {when(row.started_at || row.created_at)}
          </span>
        </span>
      </button>
      <div className="row spread">
        <div>
          <Badge tone="partial">{row.status}</Badge>
          <ReviewReason reason={row.review_reason} />
        </div>
        {row.candidate_id ? (
          <Link to={`/contacts/${row.candidate_id}`}>Open record</Link>
        ) : (
          <span className="muted small">unattributed</span>
        )}
      </div>
      {open ? <CallReviewBody callId={row.id} /> : null}
      {!row.candidate_id ? (
        <AttributeForm row={row} attribute={intakeApi.patchCall} onDone={onDone} />
      ) : null}
    </div>
  );
}

export function IntakeReview() {
  const calls = useAsync(() => intakeApi.listCalls({ review_needed: true }), []);
  // Threads sit on the same queue. IntakeThread.review_needed defaults to
  // true, so leaving them out meant every chat was flagged and none shown.
  const threads = useAsync(() => intakeApi.listThreads({ review_needed: true }), []);
  const rows = calls.data ?? [];
  const threadRows = threads.data ?? [];
  const loading = calls.loading || threads.loading;

  return (
    <div className="stack" data-testid="intake-review">
      {loading ? <Loading label="Loading review queue…" /> : null}
      <ErrorBox error={calls.error} />
      <ErrorBox error={threads.error} />
      {!loading && !rows.length && !threadRows.length ? (
        <Empty>Nothing waiting on review.</Empty>
      ) : (
        <>
          {threadRows.map((row) => (
            <ThreadRow key={row.id} row={row} onDone={() => threads.reload()} />
          ))}
          {rows.map((row) => (
            <CallRow key={row.id} row={row} onDone={() => calls.reload()} />
          ))}
        </>
      )}
    </div>
  );
}
