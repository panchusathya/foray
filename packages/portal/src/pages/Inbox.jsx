/**
 * What the employer actually said.
 *
 * The alias made us the addressee of record, so most of this mail never
 * reaches the candidate's own client except through a best-effort relay. This
 * screen is the durable copy, and for a Workday-style pursuit it is the only
 * place the verification mail is visible at all.
 *
 * Deliberately read-only. Replying from here means sending as the alias, which
 * is a different feature with its own consent question — until then the detail
 * view says which address it arrived at, so a person can answer from their own
 * client if they want to.
 */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { IconBuilding, IconTracker } from '../components/icons.jsx';
import { Empty, ErrorNote, Head, Mono, Screen, Sheet, Skeleton } from '../components/ui.jsx';
import { api } from '../lib/api.js';
import { ago } from '../lib/applications.js';
import { chipsFor, kindLabel, order, senderInitials, senderLabel } from '../lib/inbox.js';

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
              {open.alias_address ? (
                <p className="em">
                  Sent to <Mono name={open.alias_address} size="sm" /> — the address on your
                  application.
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
