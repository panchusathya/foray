/**
 * The inbox draft queue: mail somebody sent you, and a reply you could send.
 *
 * One card at a time rather than a grid. The point of the screen is reading
 * what arrived and deciding what to say back, and a table of truncated bodies
 * makes people approve without reading — which is the one failure this whole
 * feature cannot afford.
 *
 * Approving is the only action that writes to the mailbox, so it is the only
 * one that is styled as the primary button and the only one that says where
 * the draft is going.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';

function received(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function senderLabel(draft) {
  if (draft.candidate_name) return draft.candidate_name;
  if (draft.from_name) return draft.from_name;
  return draft.from_address;
}

function DraftCard({ draft, onApprove, onDismiss, busy }) {
  const [body, setBody] = useState(draft.body);
  const [open, setOpen] = useState(false);

  const inbound = (draft.inbound_body || '').trim();
  const long = inbound.length > 600;
  const shown = open || !long ? inbound : `${inbound.slice(0, 600)}…`;

  return (
    <div className="card stack draft-card">
      <div className="spread">
        <div>
          <strong>{senderLabel(draft)}</strong>
          {draft.candidate_id ? (
            <>
              {' '}
              <Link className="small" to={`/contacts/${draft.candidate_id}`}>
                open contact
              </Link>
            </>
          ) : null}
          <div className="small muted">
            {draft.from_address}
            {received(draft.received_at) ? ` · ${received(draft.received_at)}` : ''}
          </div>
        </div>
        <div className="row">
          {draft.candidate_id ? <Badge>contact</Badge> : <Badge tone="partial">new</Badge>}
          {draft.edited ? <Badge tone="accent">edited</Badge> : null}
        </div>
      </div>

      <div>
        <div className="small muted">Subject</div>
        <div>{draft.subject || '(no subject)'}</div>
      </div>

      <div className="quoted">
        {shown || <span className="muted">(no body)</span>}
        {long ? (
          <div>
            <button type="button" className="tiny ghost" onClick={() => setOpen(!open)}>
              {open ? 'Show less' : 'Show the whole message'}
            </button>
          </div>
        ) : null}
      </div>

      {draft.drafting ? (
        <p className="small muted">Writing a draft…</p>
      ) : draft.needs_a_human ? (
        // The model returned nothing on purpose. Saying so is the point:
        // a generic paragraph here is worse than an empty box.
        <p className="small" style={{ color: 'var(--partial)' }}>
          This one is worth writing yourself — the draft was left blank on purpose.
        </p>
      ) : null}

      <div>
        <div className="small muted">Your reply</div>
        <textarea
          className="draft-body"
          value={body}
          rows={6}
          onChange={(event) => setBody(event.target.value)}
          placeholder="Write the reply…"
        />
      </div>

      {draft.error ? <ErrorBox error={{ message: draft.error }} /> : null}

      <div className="row spread">
        <button
          type="button"
          className="tiny ghost"
          onClick={() => onDismiss(draft)}
          disabled={busy}
        >
          Dismiss
        </button>
        <div className="row">
          {draft.web_link ? (
            <a className="badge" href={draft.web_link} target="_blank" rel="noreferrer noopener">
              Open the email
            </a>
          ) : null}
          <button
            type="button"
            className="primary"
            onClick={() => onApprove(draft, body)}
            disabled={busy || !body.trim()}
          >
            Put in my Drafts
          </button>
        </div>
      </div>
    </div>
  );
}

export function InboxDraftsPage() {
  const queue = useAsync(() => api.listInboxDrafts(), []);
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState('');
  const [note, setNote] = useState('');

  async function act(draft, run, message) {
    setBusyId(draft.id);
    setError(null);
    try {
      await run();
      setNote(message);
      queue.reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusyId('');
    }
  }

  const approve = (draft, body) =>
    act(
      draft,
      () => api.approveInboxDraft(draft.id, { body }),
      `Draft for ${senderLabel(draft)} is in your mailbox — edit and send it from there.`,
    );

  const dismiss = (draft) =>
    act(draft, () => api.dismissInboxDraft(draft.id), `Dismissed ${senderLabel(draft)}.`);

  const drafts = queue.data?.drafts ?? [];

  return (
    <div className="stack">
      <PageHeader
        title="Inbox drafts"
        subtitle="Mail that arrived, and a reply you can edit. Nothing is sent for you."
      />

      <ErrorBox error={error} />
      {note ? <div className="card small">{note}</div> : null}

      {queue.loading ? <Loading /> : null}
      <ErrorBox error={queue.error} />

      {!queue.loading && !drafts.length ? (
        <Empty>
          Nothing waiting. Drafts appear here when someone emails a connected mailbox —
          receipts, newsletters and automated mail are skipped.
        </Empty>
      ) : null}

      {drafts.map((draft) => (
        <DraftCard
          key={draft.id}
          draft={draft}
          busy={busyId === draft.id}
          onApprove={approve}
          onDismiss={dismiss}
        />
      ))}
    </div>
  );
}
