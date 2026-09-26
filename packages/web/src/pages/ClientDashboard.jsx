/**
 * The client's whole app: one page, the people we shared, no sidebar.
 *
 * Rendered by App.jsx instead of the recruiter Shell whenever the session is
 * a client login, so none of the recruiter pages, the project provider or
 * their API calls are ever mounted. The server refuses a client token on
 * those routes regardless; this just means the client never sees a broken
 * screen trying.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { ClientPipeline } from '../components/ClientPipeline.jsx';
import { BrandMark, ErrorBox, Loading } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAuth } from '../lib/auth.jsx';
import { usePolledRows } from '../lib/clientPolling.js';

export function ClientDashboardPage() {
  const { user, logout } = useAuth();
  const load = useCallback(() => api.clientSubmissions(), []);
  const { rows, setRows, error, loading, syncedAt, refresh } = usePolledRows(load);

  const pipelineApi = useMemo(
    () => ({
      loadDetail: (id) => api.clientSubmission(id),
      setStage: (id, stage) => api.clientSetStage(id, stage),
      addNote: (id, body) => api.clientAddNote(id, body),
      deleteNote: (noteId) => api.clientDeleteNote(noteId),
    }),
    [],
  );

  const [, tick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 15000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="client-shell cp-ui">
      <header className="client-header">
        <div className="brand brand-lockup">
          <BrandMark />
          <span>Foray</span>
          <span className="muted client-for">× {user?.client_name}</span>
        </div>
        <div className="row">
          <span className="muted small">{user?.name}</span>
          <button type="button" className="tiny ghost" onClick={logout}>
            Log out
          </button>
        </div>
      </header>
      <main className="client-main">
        <div className="page-header spread">
          <div>
            <h1>Candidates</h1>
          </div>
          <div className="row">
            <span className="muted small" data-testid="synced-at">
              {syncedAt ? `Synced ${new Date(syncedAt).toLocaleTimeString()}` : ''}
            </span>
            <button type="button" className="tiny ghost" onClick={refresh}>
              Refresh
            </button>
          </div>
        </div>
        <ErrorBox error={error} />
        {loading && !rows.length ? (
          <Loading label="Loading candidates…" />
        ) : (
          <ClientPipeline
            side="client"
            rows={rows}
            api={pipelineApi}
            clientName={user?.client_name ?? 'Client'}
            onRowsChange={setRows}
            exportName={`${(user?.client_name ?? 'client').toLowerCase()}-candidates`}
          />
        )}
      </main>
    </div>
  );
}
