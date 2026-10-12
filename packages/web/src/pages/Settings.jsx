import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { SendingPace } from '../components/SendingPace.jsx';
import { MAIL_PROVIDERS, api, providerLabel } from '../lib/api.js';
import { activityVisible, rowVisible } from '../lib/mailboxActivity.js';
import { useCurrentProject } from '../lib/project.jsx';
import { useAsync } from '../lib/useAsync.js';

/**
 * Connected sending mailboxes, across every provider.
 *
 * Mailboxes are a tenant-wide pool: sequences rotate across all of them by
 * default, so the per-mailbox daily cap here is the real deliverability
 * control. It is shown next to the rolling 24h count so the number means
 * something while you are setting it.
 */
/**
 * Two different zeros, which looked identical and were the whole complaint.
 *
 * "0 imported" because nobody is in scope is a setup problem; "0 imported"
 * because the mail is already here is success. Neither should read as a
 * broken button.
 */
function backfillSummary(backfill) {
  if (!backfill.total) return backfill.note || 'nothing to import';
  const checked = `${backfill.done} of ${backfill.total} contacts checked`;
  if (backfill.imported) {
    return `${checked}, ${backfill.imported} email${
      backfill.imported === 1 ? '' : 's'
    } imported`;
  }
  return backfill.status === 'done'
    ? `${checked} — already up to date, nothing new to import`
    : checked;
}

export function SettingsPage() {
  const status = useAsync(() => api.mailboxes(), []);
  const { sending, reloadSending } = useCurrentProject();
  const [error, setError] = useState(null);
  const [syncNote, setSyncNote] = useState('');
  // What the import and the sweep are doing. Polled rather than pushed --
  // there is no realtime plumbing here -- and only while something is
  // actually running, so an idle Settings page makes no requests.
  const [activity, setActivity] = useState([]);
  const [syncing, setSyncing] = useState('');
  const [caps, setCaps] = useState({});

  // The OAuth consent happens in another tab; refresh when the user comes back.
  useEffect(() => {
    const onFocus = () => status.reload();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, []);

  async function connect(provider) {
    setError(null);
    try {
      const { auth_url: authUrl } = await api.mailboxConnect(provider);
      window.open(authUrl, '_blank', 'noopener');
    } catch (caught) {
      setError(caught);
    }
  }

  async function disconnect(connectionId) {
    setError(null);
    try {
      await api.mailboxDisconnect(connectionId);
      status.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  async function syncNow(connectionId) {
    // Concurrent syncs both read-then-write the same folder cursor, so the
    // second one can overwrite the first's and re-import the same replies.
    if (syncing) return;
    setError(null);
    setSyncing(connectionId);
    try {
      const result = await api.mailboxSync(connectionId);
      setSyncNote(
        result.initialized
          ? 'Sync initialised — watching for new replies from now on.'
          : `Synced: ${result.matched} repl${result.matched === 1 ? 'y' : 'ies'} matched (${result.seen} new emails).`,
      );
      status.reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setSyncing('');
    }
  }

  async function toggleThreadSync(connection) {
    const enabling = !connection.thread_sync_enabled;
    if (
      enabling &&
      !window.confirm(
        'Copy candidate correspondence from this mailbox into Foray? Only emails ' +
          'with people already in a project or sequence are stored — nothing else ' +
          'in the mailbox is read into the CRM.',
      )
    ) {
      return;
    }
    setError(null);
    try {
      await api.mailboxThreadSync(connection.id, enabling);
      status.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  async function toggleSending(connection) {
    setError(null);
    try {
      await api.mailboxSending(connection.id, { send_enabled: !connection.send_enabled });
      status.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  async function toggleOrgSending() {
    const paused = Boolean(sending?.paused);
    if (!paused) {
      const n = sending?.held_count ?? 0;
      if (
        !window.confirm(
          `Pause all sequence email? ${n} in flight will be held. Stops new sends; anything already handed to the provider will finish. Follow-up reminders and draft-ready pings keep working.`,
        )
      ) {
        return;
      }
    }
    setError(null);
    try {
      await api.patchOrgSending({ paused: !paused });
      await reloadSending();
    } catch (caught) {
      setError(caught);
    }
  }

  async function saveCap(connection) {
    const raw = caps[connection.id];
    if (raw === undefined) return;
    const value = Number.parseInt(raw, 10);
    if (Number.isNaN(value) || value < 0) {
      setError(new Error('Daily cap must be zero or more. Zero stops this mailbox sending.'));
      return;
    }
    setError(null);
    try {
      await api.mailboxSending(connection.id, { daily_send_cap: value });
      setCaps((current) => {
        const next = { ...current };
        delete next[connection.id];
        return next;
      });
      status.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  useEffect(() => {
    let cancelled = false;
    let timer;
    let elapsed = 0;

    async function tick() {
      if (cancelled) return;
      let busy = false;
      try {
        const rows = await api.mailboxActivity();
        if (cancelled) return;
        setActivity(rows);
        busy = rows.some((row) =>
          ['queued', 'running'].includes(row.backfill.status) ||
          ['queued', 'running'].includes(row.discovery.status),
        );
      } catch {
        // A failed poll is not worth a banner; the next one may work.
      }
      if (cancelled || !busy) return;
      elapsed += 5000;
      // Back off, then stop rather than polling a stuck job forever.
      if (elapsed > 10 * 60_000) return;
      timer = setTimeout(tick, elapsed > 2 * 60_000 ? 15_000 : 5_000);
    }

    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [syncNote]);

  async function backfill(connection) {
    if (
      !window.confirm(
        'Import the last 12 months of email history with known candidates? ' +
          'This runs in the background and can take a while.',
      )
    ) {
      return;
    }
    setError(null);
    try {
      const result = await api.mailboxBackfill(connection.id);
      setSyncNote(
        result.contacts
          ? `History import queued for ${result.contacts} contact${
              result.contacts === 1 ? '' : 's'
            } — progress below.`
          : 'Nothing to import yet: no contacts are in scope for this mailbox. ' +
            'Add people to a project or sweep for contacts first.',
      );
    } catch (caught) {
      setError(caught);
    }
  }

  async function discover(connection) {
    // Says what it actually does. Thread sync's own description talks about
    // "known candidates", which is no longer the whole story once a sweep can
    // surface people who are in no project and no sequence.
    if (
      !window.confirm(
        `Scan ${connection.address} for people you have emailed who are not in ` +
          'Contacts yet? It reads addresses, dates and message counts — never ' +
          'message content — and stages them for you to review.',
      )
    ) {
      return;
    }
    setError(null);
    try {
      await api.startDiscovery(connection.id);
      setSyncNote('Sweep queued — watch progress in Contacts → Discovered.');
    } catch (caught) {
      setError(caught);
    }
  }

  const data = status.data;
  const providers = data?.providers ?? [];
  const connections = data?.connections ?? [];
  const poolCapacity = connections
    .filter((c) => c.status === 'active' && c.send_enabled)
    .reduce((total, c) => total + c.daily_send_cap, 0);

  return (
    <div className="stack">
      <PageHeader
        title="Settings"
        subtitle="Sending mailboxes. Outreach goes out as you, from your own addresses."
        actions={
          <Link className="dl-nav-link" to="/deliverability" data-testid="deliverability-link">
            Deliverability →
          </Link>
        }
      />
      <ErrorBox error={error} />
      {syncNote ? <div className="card small">{syncNote}</div> : null}

      <div className="card spread" data-testid="org-sending">
        <div>
          <strong>Sequence email</strong>
          <div className="small muted">
            Candidate-facing outreach only. Follow-up reminders and draft-ready pings keep
            working. Stops new sends; anything already handed to the provider will finish.
          </div>
        </div>
        <button
          type="button"
          className="tiny ghost"
          data-testid="org-sending-toggle"
          onClick={toggleOrgSending}
        >
          {sending?.paused ? 'Sending: off' : 'Sending: on'}
        </button>
      </div>

      {sending ? <SendingPace sending={sending} onSaved={reloadSending} /> : null}

      {/* A finished run keeps its "done" status forever, so filtering on
          "not idle" left August's sweep on screen in late September. See
          lib/mailboxActivity.js for which runs are still worth a card. */}
      {activity
        .filter((row) => rowVisible(row))
        .map((row) => (
          <div className="card small stack" key={row.mailbox_id} data-testid="mailbox-activity">
            <strong>{row.address}</strong>
            {activityVisible(row.backfill) ? (
              <span data-testid="backfill-progress">
                History import: {row.backfill.status} —{' '}
                {backfillSummary(row.backfill)}
                {row.backfill.error ? ` · ${row.backfill.error}` : ''}
              </span>
            ) : null}
            {activityVisible(row.discovery) ? (
              <span data-testid="discovery-progress">
                Contact sweep: {row.discovery.status} —{' '}
                {row.discovery.scanned.toLocaleString()} messages scanned,{' '}
                {row.discovery.found.toLocaleString()} people found
                {row.discovery.error ? ` · ${row.discovery.error}` : ''}
              </span>
            ) : null}
          </div>
        ))}
      {status.loading ? <Loading /> : null}

      {data ? (
        <>
          <div className="card stack">
            <div className="spread">
              <div>
                <strong>Add a mailbox</strong>
                <div className="small muted">
                  Sequences spread sends across every enabled mailbox, so more
                  addresses means more volume without pushing any one of them
                  harder.
                </div>
              </div>
              <div className="row">
                {providers.map((provider) => (
                  <button
                    key={provider.name}
                    type="button"
                    className="primary"
                    disabled={!provider.configured}
                    title={
                      provider.configured
                        ? `Redirect URI: ${provider.redirect_uri}`
                        : 'Not configured on the server'
                    }
                    onClick={() => connect(provider.name)}
                  >
                    Connect {providerLabel(provider.name)}
                  </button>
                ))}
              </div>
            </div>

            {providers
              .filter((provider) => !provider.configured)
              .map((provider) => (
                <div className="small muted" key={provider.name}>
                  {providerLabel(provider.name)} needs{' '}
                  {(MAIL_PROVIDERS[provider.name]?.envVars ?? []).map((name, index) => (
                    <span key={name}>
                      {index ? ' and ' : ''}
                      <code>{name}</code>
                    </span>
                  ))}{' '}
                  set on the server. Redirect URI: <code>{provider.redirect_uri}</code>
                </div>
              ))}
          </div>

          <div className="card stack">
            <div className="spread">
              <strong className="small">Connected mailboxes</strong>
              {connections.length ? (
                <span className="small muted">
                  Pool capacity: {poolCapacity} sends / 24h
                </span>
              ) : null}
            </div>

            {connections.length ? (
              <div className="stack">
                {connections.map((connection) => (
                  <div className="stack" style={{ gap: 4 }} key={connection.id}>
                    <div className="spread">
                      <div>
                        <strong className="small">{connection.address}</strong>{' '}
                        <Badge>{providerLabel(connection.provider)}</Badge>
                        <div className="small muted">
                          {connection.display_name || providerLabel(connection.provider)}
                          {connection.last_synced_at
                            ? ` · replies checked ${new Date(connection.last_synced_at).toLocaleString()}`
                            : ' · replies not checked yet'}
                          {connection.error ? ` — ${connection.error}` : ''}
                        </div>
                      </div>
                      <div className="row">
                        <Badge tone={connection.status === 'active' ? 'strong' : ''}>
                          {connection.status}
                        </Badge>
                        {connection.status === 'active' ? (
                          <button
                            type="button"
                            className="tiny"
                            disabled={!!syncing}
                            onClick={() => syncNow(connection.id)}
                          >
                            {syncing === connection.id ? 'Syncing…' : 'Sync now'}
                          </button>
                        ) : null}
                        {connection.status === 'active' ? (
                          <button
                            type="button"
                            className="tiny"
                            data-testid="thread-sync-toggle"
                            title="Store email with people in the CRM, and allow sweeping this mailbox for contacts you have not added yet"
                            onClick={() => toggleThreadSync(connection)}
                          >
                            {connection.thread_sync_enabled
                              ? 'Thread sync: on'
                              : 'Thread sync: off'}
                          </button>
                        ) : null}
                        {connection.status === 'active' && connection.thread_sync_enabled ? (
                          <button
                            type="button"
                            className="tiny ghost"
                            onClick={() => backfill(connection)}
                          >
                            Import history
                          </button>
                        ) : null}
                        {connection.status === 'active' && connection.thread_sync_enabled ? (
                          <button
                            type="button"
                            className="tiny ghost"
                            data-testid="discover-sweep"
                            title="Find people you have emailed who are not in Contacts yet"
                            onClick={() => discover(connection)}
                          >
                            Find contacts
                          </button>
                        ) : null}
                        {connection.status !== 'revoked' ? (
                          <button
                            type="button"
                            className="tiny ghost"
                            onClick={() => disconnect(connection.id)}
                          >
                            Disconnect
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="tiny"
                            onClick={() => connect(connection.provider)}
                          >
                            Reconnect
                          </button>
                        )}
                      </div>
                    </div>

                    {connection.status === 'active' ? (
                      <div
                        className="row wrap small muted"
                        style={{ alignItems: 'center' }}
                        data-testid="mailbox-sending-controls"
                      >
                        <span>Daily cap</span>
                        <input
                          type="number"
                          min="0"
                          style={{ width: 90 }}
                          data-testid="daily-cap-input"
                          value={caps[connection.id] ?? connection.daily_send_cap}
                          onChange={(event) =>
                            setCaps({ ...caps, [connection.id]: event.target.value })
                          }
                        />
                        {caps[connection.id] !== undefined &&
                        String(caps[connection.id]) !== String(connection.daily_send_cap) ? (
                          <button
                            type="button"
                            className="tiny"
                            onClick={() => saveCap(connection)}
                          >
                            Save cap
                          </button>
                        ) : null}
                        <span>
                          {connection.sent_last_24h} sent in the last 24h
                          {connection.daily_send_cap > 0
                            ? ` of ${connection.daily_send_cap}`
                            : ' · cap 0, this mailbox is paused'}
                        </span>
                        <button
                          type="button"
                          className="tiny ghost"
                          data-testid="sending-toggle"
                          onClick={() => toggleSending(connection)}
                        >
                          {connection.send_enabled ? 'Sending: on' : 'Sending: off'}
                        </button>
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : (
              <div className="small muted">No mailbox connected yet.</div>
            )}
          </div>
        </>
      ) : null}
    </div>
  );
}
