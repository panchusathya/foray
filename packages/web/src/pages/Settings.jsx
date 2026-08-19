import { useEffect, useState } from 'react';

import { Badge, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';

export function SettingsPage() {
  const status = useAsync(() => api.microsoftStatus(), []);
  const [error, setError] = useState(null);
  const [syncNote, setSyncNote] = useState('');
  const [syncing, setSyncing] = useState('');

  // The OAuth consent happens in another tab; refresh when the user comes back.
  useEffect(() => {
    const onFocus = () => status.reload();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, []);

  async function connect() {
    setError(null);
    try {
      const { auth_url: authUrl } = await api.microsoftConnect();
      window.open(authUrl, '_blank', 'noopener');
    } catch (caught) {
      setError(caught);
    }
  }

  async function disconnect(connectionId) {
    setError(null);
    try {
      await api.microsoftDisconnect(connectionId);
      status.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  async function syncNow(connectionId) {
    // Concurrent syncs both read-then-write inbox_delta_link, so the second
    // one can overwrite the first's cursor and re-import the same replies.
    if (syncing) return;
    setError(null);
    setSyncing(connectionId);
    try {
      const result = await api.microsoftSync(connectionId);
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
      await api.microsoftThreadSync(connection.id, enabling);
      status.reload();
    } catch (caught) {
      setError(caught);
    }
  }

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
      await api.microsoftBackfill(connection.id);
      setSyncNote('History import queued — it runs in the background.');
    } catch (caught) {
      setError(caught);
    }
  }

  const data = status.data;

  return (
    <div className="stack">
      <PageHeader
        title="Settings"
        subtitle="Connected accounts. Outreach sends as you, from your own mailbox."
      />
      <ErrorBox error={error} />
      {syncNote ? <div className="card small">{syncNote}</div> : null}
      {status.loading ? <Loading /> : null}

      {data ? (
        <div className="card stack">
          <div className="spread">
            <div>
              <strong>Outlook</strong>
              <div className="small muted">
                Send sequences from your own Microsoft 365 mailbox, so outreach
                looks like the personal email it is.
              </div>
            </div>
            {data.configured ? (
              <button type="button" className="primary" onClick={connect}>
                Connect Outlook
              </button>
            ) : (
              <Badge>not configured</Badge>
            )}
          </div>

          {!data.configured ? (
            <div className="small muted">
              Set <code>MS_CLIENT_ID</code> and <code>MS_CLIENT_SECRET</code> on the
              server (Azure app registration) to enable this. Redirect URI:{' '}
              <code>{data.redirect_uri}</code>
            </div>
          ) : null}

          {data.connections.length ? (
            <div className="stack">
              {data.connections.map((connection) => (
                <div className="spread" key={connection.id}>
                  <div>
                    <strong className="small">{connection.address}</strong>
                    <div className="small muted">
                      {connection.display_name || 'Outlook mailbox'}
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
                        title="Copy emails with known candidates into their CRM timelines"
                        onClick={() => toggleThreadSync(connection)}
                      >
                        {connection.thread_sync_enabled ? 'Thread sync: on' : 'Thread sync: off'}
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
                    {connection.status !== 'revoked' ? (
                      <button
                        type="button"
                        className="tiny ghost"
                        onClick={() => disconnect(connection.id)}
                      >
                        Disconnect
                      </button>
                    ) : (
                      <button type="button" className="tiny" onClick={connect}>
                        Reconnect
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : data.configured ? (
            <div className="small muted">No mailbox connected yet.</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
