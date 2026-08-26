/**
 * What the employer actually said.
 *
 * The alias made us the addressee of record, so most of this mail never
 * reaches the candidate's own client except through a best-effort relay. This
 * screen is the durable copy, and for a Workday-style pursuit it is the only
 * place the verification mail is visible at all.
 *
 * Replies go out as the alias, not from the candidate's own address. They
 * applied as anna+<token>@, so that is the only sender the employer has a
 * record of, and a reply from a personal inbox arrives as a stranger on a
 * thread nobody can match. It also keeps the employer's answer coming back to
 * us, which is what stops this screen going dark the moment a real
 * conversation starts.
 *
 * A message with no one behind it — a verification robot — says so instead of
 * offering a box that would bounce.
 */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { IconBuilding, IconTracker } from '../components/icons.jsx';
import { Empty, ErrorNote, Head, Note, Screen, Sheet, Skeleton } from '../components/ui.jsx';
import { api } from '../lib/api.js';
import { ago } from '../lib/applications.js';
import {
  canReply,
  chipsFor,
  kindLabel,
  order,
  senderInitials,
  senderLabel,
} from '../lib/inbox.js';

export function TrackerTabs({ current }) {
  return (
    <div className="segmented" role="group" aria-label="Tracker view">
      <Link to="/inbox" className={current === 'inbox' ? 'on' : ''}>
        Inbox
      </Link>
      <Link to="/applications" className={current === 'pipeline' ? 'on' : ''}>
        Pipeline
      </Link>
    </div>
  );
}

function MessageRow({ message, onOpen }) {
  const label = kindLabel(message.kind);
  return (
    <button type="button" className="msg-row" onClick={() => onOpen(message)}>
      <span className="mono round" aria-hidden="true">
        {senderInitials(message)}
      </span>
      <span className="grow truncate">
        <span className="msg-from truncate">{senderLabel(message)}</span>
        <span className={`msg-subject truncate${message.read ? '' : ' unread'}`}>
          {message.subject || '(no subject)'}
        </span>
        <span className="msg-preview truncate">{message.preview}</span>
      </span>
      <span className="msg-meta">
        <span className="em">{ago(message.received_at)}</span>
        {message.read ? null : <span className="dot" aria-label="Unread" />}
        {label ? <span className={`tag ${message.kind}`}>{label}</span> : null}
      </span>
    </button>
  );
}

function Composer({ message, onSent }) {
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);
  const [sent, setSent] = useState(false);

  if (!canReply(message)) {
    return (
      <Note tone="info">
        {/* Saying so beats a reply box that bounces. */}
        This came from a no-reply address, so there is nobody to answer here.
      </Note>
    );
  }

  if (sent) {
    return <Note tone="good">Sent. Their answer will land back in this thread.</Note>;
  }

  const send = async () => {
    const body = text.trim();
    if (!body || sending) return;
    setSending(true);
    setError(null);
    try {
      await api.trackerReply(message.id, body);
      setSent(true);
      setText('');
      onSent?.();
    } catch (err) {
      setError(err);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="composer">
      <label className="em" htmlFor="reply-body">
        Reply to {message.reply_to}
      </label>
      <textarea
        id="reply-body"
        rows={5}
        value={text}
        placeholder="Tuesday works for me…"
        onChange={(event) => setText(event.target.value)}
        disabled={sending}
      />
      <ErrorNote error={error} />
      <div className="composer-foot">
        <span className="em">
          {/* The one thing worth saying here: it does not come from their
              Gmail, and that is deliberate. */}
          Sends as {message.alias_address || 'your application address'}, so they recognise
          you.
        </span>
        <button type="button" className="btn" onClick={send} disabled={sending || !text.trim()}>
          {sending ? 'Sending…' : 'Send'}
        </button>
      </div>
    </div>
  );
}

export function InboxPage() {
  const [state, setState] = useState({ loading: true, error: null, messages: [], counts: {} });
  const [kind, setKind] = useState('all');
  const [open, setOpen] = useState(null);

  const load = useCallback(async (which) => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const body = await api.trackerMessages(which === 'all' ? '' : which);
      setState({
        loading: false,
        error: null,
        messages: order(body.messages || []),
        counts: body.counts || {},
      });
    } catch (error) {
      setState({ loading: false, error, messages: [], counts: {} });
    }
  }, []);

  useEffect(() => {
    load(kind);
  }, [kind, load]);

  const openMessage = useCallback(
    async (message) => {
      // Optimistic: the dot going out the moment you tap is the whole point of
      // a read receipt, and the server call only confirms it.
      setOpen({ ...message, body: '', loading: true });
      setState((prev) => ({
        ...prev,
        messages: prev.messages.map((row) =>
          row.id === message.id ? { ...row, read: true } : row,
        ),
      }));
      try {
        const full = await api.trackerMessage(message.id);
        setOpen({ ...full, loading: false });
      } catch (error) {
        setOpen({ ...message, loading: false, error });
      }
    },
    [],
  );

  const chips = chipsFor(state.counts);

  return (
    <Screen wide>
      <Head
        title="Inbox"
        sub="Replies, interviews and verifications from employers."
        action={<TrackerTabs current="inbox" />}
      />

      {chips.length > 1 ? (
        <div className="chips" role="group" aria-label="Filter">
          {chips.map((chip) => (
            <button
              key={chip.key}
              type="button"
              className={`chip ${chip.tone}${kind === chip.key ? ' on' : ''}`}
              aria-pressed={kind === chip.key}
              onClick={() => setKind(chip.key)}
            >
              {chip.label}
              {chip.count ? <span className="n">{chip.count}</span> : null}
            </button>
          ))}
        </div>
      ) : null}

      <ErrorNote error={state.error} />

      {state.loading ? (
        <Skeleton />
      ) : state.messages.length ? (
        <div className="msg-list">
          {state.messages.map((message) => (
            <MessageRow key={message.id} message={message} onOpen={openMessage} />
          ))}
        </div>
      ) : (
        <Empty
          title={kind === 'all' ? 'Nothing yet' : 'Nothing in this filter'}
          glyph={<IconTracker />}
        >
          {kind === 'all'
            ? 'When an employer replies to one of your applications, it lands here.'
            : 'Try All.'}
        </Empty>
      )}

      {open ? (
        <Sheet
          title={open.subject || '(no subject)'}
          sub={`${senderLabel(open)} · ${ago(open.received_at)}`}
          onClose={() => setOpen(null)}
        >
          {open.loading ? (
            <Skeleton count={1} />
          ) : (
            <>
              {open.application_id ? (
                <Link to={`/applications/${open.application_id}`} className="btn small ghost">
                  <IconBuilding />
                  {open.company || 'View the application'}
                </Link>
              ) : null}
              <pre className="msg-body">{open.body}</pre>
              <Composer message={open} onSent={() => load(kind)} />
              {open.alias_address ? (
                <p className="em">
                  {/* The literal address, not a monogram: the whole point is
                      that a person can read it and answer from their own
                      client if they would rather. */}
                  Sent to <code className="addr">{open.alias_address}</code> — the address on
                  your application.
                  {open.relayed ? ' A copy went to your own inbox.' : ''}
                </p>
              ) : null}
            </>
          )}
        </Sheet>
      ) : null}
    </Screen>
  );
}
