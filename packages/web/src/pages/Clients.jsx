/**
 * Our side of the client dashboards.
 *
 * `/clients` lists the companies with a login; `/clients/:clientId` is the
 * same grid the client sees, plus sharing, bulk moves and login management.
 * A stage the client moves shows here on the next poll, tagged with their
 * name in the Updated column, so "what has Flowgen done since yesterday" is
 * readable without opening every row.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { ClientPipeline } from '../components/ClientPipeline.jsx';
import { Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { usePolledRows } from '../lib/clientPolling.js';
import { STAGES, parsePastedRows, relativeTime, stageLabel } from '../lib/clientStages.js';

function NewClientForm({ onCreated }) {
  const [form, setForm] = useState({ name: '', username: '', password: '' });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const account = await api.createClient({ name: form.name.trim() });
      if (form.username.trim()) {
        await api.createClientLogin(account.id, {
          username: form.username.trim(),
          password: form.password,
        });
      }
      onCreated(account);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={submit} data-testid="new-client-form">
      <h3 style={{ margin: 0 }}>New client dashboard</h3>
      <ErrorBox error={error} />
      <div className="row wrap cp-inline-controls">
        <input
          type="text"
          placeholder="Company, e.g. Flowgen"
          value={form.name}
          onChange={(event) => setForm({ ...form, name: event.target.value })}
          required
        />
        <input
          type="text"
          placeholder="Login username"
          value={form.username}
          onChange={(event) => setForm({ ...form, username: event.target.value })}
          autoComplete="off"
        />
        <input
          type="password"
          placeholder="Password (8+ characters)"
          value={form.password}
          onChange={(event) => setForm({ ...form, password: event.target.value })}
          minLength={form.username ? 8 : undefined}
          required={Boolean(form.username)}
          autoComplete="new-password"
        />
        <button type="submit" className="primary" disabled={busy}>
          {busy ? 'Creating…' : 'Create'}
        </button>
      </div>
    </form>
  );
}

export function ClientsPage() {
  const navigate = useNavigate();
  const [accounts, setAccounts] = useState(null);
  const [error, setError] = useState(null);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    api.listClients().then(setAccounts).catch(setError);
  }, []);

  return (
    <div className="stack cp-ui">
      <PageHeader
        title="Clients"
        subtitle="Companies with their own login onto the candidates we have shared with them."
        actions={
          <button type="button" className="primary" onClick={() => setAdding((v) => !v)}>
            {adding ? 'Cancel' : 'New client'}
          </button>
        }
      />
      <ErrorBox error={error} />
      {adding ? <NewClientForm onCreated={(a) => navigate(`/clients/${a.id}`)} /> : null}
      {accounts === null ? (
        <Loading />
      ) : accounts.length === 0 ? (
        <Empty>No client dashboards yet.</Empty>
      ) : (
        <div className="grid cp-client-cards">
          {accounts.map((account) => (
            <Link
              key={account.id}
              to={`/clients/${account.id}`}
              className="card cp-client-card"
              data-testid="client-card"
            >
              <div className="spread">
                <h3 style={{ margin: 0 }}>{account.name}</h3>
                {account.client_activity_7d ? (
                  <span className="badge accent">{account.client_activity_7d} updates this week</span>
                ) : null}
              </div>
              <div className="muted small">
                {account.total} shared ·{' '}
                {account.logins.map((l) => l.username).join(', ') || 'no login yet'}
              </div>
              <div className="row wrap small">
                {STAGES.filter((s) => account.stage_counts[s.id]).map((s) => (
                  <span key={s.id} className="badge">
                    {s.short} {account.stage_counts[s.id]}
                  </span>
                ))}
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function ShareForm({ accountId, onShared }) {
  const [text, setText] = useState('');
  const [role, setRole] = useState('');
  const [stage, setStage] = useState('');
  const [projectId, setProjectId] = useState('');
  const [projects, setProjects] = useState([]);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const parsed = useMemo(() => parsePastedRows(text), [text]);

  useEffect(() => {
    api
      .listProjects()
      .then(setProjects)
      .catch(() => setProjects([]));
  }, []);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const outcome = await api.shareWithClient(accountId, {
        entries: parsed.map(({ full_name, linkedin_url, stage: s }) => ({
          full_name,
          linkedin_url,
          stage: s,
        })),
        project_id: projectId || null,
        role_title: role,
        stage,
      });
      setResult(outcome);
      setText('');
      setProjectId('');
      onShared();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={submit} data-testid="share-form">
      <div>
        <h3 style={{ margin: 0 }}>Share candidates</h3>
        <p className="muted small" style={{ margin: '4px 0 0' }}>
          Paste one per line: a LinkedIn URL, a name, or rows straight from a spreadsheet
          (name, LinkedIn, status in any column order). A status like “rejected” or “1st
          round” sets the stage. Pasting someone already shared updates them rather than
          adding a duplicate.
        </p>
      </div>
      <ErrorBox error={error} />
      <textarea
        rows={6}
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder={
          'https://www.linkedin.com/in/priya-raman\nJo Bloggs\thttps://www.linkedin.com/in/jo-bloggs\tRejected'
        }
        data-testid="share-input"
      />
      {parsed.length ? (
        <div className="small cp-parse-preview" data-testid="share-preview">
          {parsed.length} to share
          {parsed.some((p) => p.stage)
            ? ` · ${parsed.filter((p) => p.stage).length} with a status`
            : ''}
          <ul>
            {parsed.slice(0, 5).map((p) => (
              <li key={p.line}>
                {p.full_name || p.linkedin_url}
                {p.stage ? <span className="muted"> → {stageLabel(p.stage)}</span> : null}
              </li>
            ))}
            {parsed.length > 5 ? <li className="muted">…and {parsed.length - 5} more</li> : null}
          </ul>
        </div>
      ) : null}
      <div className="row wrap cp-inline-controls">
        <input
          type="text"
          placeholder="Role (blank = from their sequence)"
          value={role}
          onChange={(event) => setRole(event.target.value)}
        />
        <select value={stage} onChange={(event) => setStage(event.target.value)}>
          <option value="">Stage: In process (or as pasted)</option>
          {STAGES.map((s) => (
            <option key={s.id} value={s.id}>
              Stage for all: {s.label}
            </option>
          ))}
        </select>
        {projects.length ? (
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            <option value="">…and/or add a whole project pipeline</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        ) : null}
        <button
          type="submit"
          className="primary"
          disabled={busy || (!parsed.length && !projectId)}
        >
          {busy ? 'Sharing…' : 'Share'}
        </button>
      </div>
      {result ? (
        <div className="small" data-testid="share-result">
          {result.added} added · {result.updated} updated · {result.unchanged} unchanged
          {result.skipped ? ` · ${result.skipped} skipped` : ''}
          {result.results.some((r) => r.outcome === 'added' && r.matched_contact)
            ? ` · ${result.results.filter((r) => r.outcome === 'added' && r.matched_contact).length} matched to existing contacts`
            : ''}
        </div>
      ) : null}
    </form>
  );
}

function LoginsPanel({ account, onChange }) {
  const [error, setError] = useState(null);
  const [form, setForm] = useState({ username: '', password: '' });

  async function run(action) {
    setError(null);
    try {
      await action();
      onChange();
    } catch (caught) {
      setError(caught);
    }
  }

  return (
    <div className="card stack" data-testid="logins-panel">
      <h3 style={{ margin: 0 }}>Logins</h3>
      <ErrorBox error={error} />
      {account.logins.map((login) => (
        <div key={login.id} className="spread small">
          <span>
            <strong>{login.username}</strong>{' '}
            {login.name && login.name !== login.username ? (
              <span className="muted">shown as {login.name} </span>
            ) : null}
            {login.is_active ? null : <span className="badge miss">disabled</span>}{' '}
            <span className="muted">
              {login.last_login_at
                ? `last in ${relativeTime(login.last_login_at)}`
                : 'never logged in'}
            </span>
          </span>
          <span className="row">
            <button
              type="button"
              className="tiny ghost"
              onClick={() => {
                const password = window.prompt(`New password for ${login.username} (8+ characters)`);
                if (password) run(() => api.updateClientLogin(login.id, { password }));
              }}
            >
              Reset password
            </button>
            <button
              type="button"
              className="tiny ghost"
              title="The name their notes and stage moves are signed with"
              onClick={() => {
                const name = window.prompt(`Display name for ${login.username}`, login.name);
                if (name && name.trim()) run(() => api.updateClientLogin(login.id, { name: name.trim() }));
              }}
            >
              Rename
            </button>
            <button
              type="button"
              className="tiny ghost"
              onClick={() => run(() => api.updateClientLogin(login.id, { is_active: !login.is_active }))}
            >
              {login.is_active ? 'Disable' : 'Enable'}
            </button>
          </span>
        </div>
      ))}
      <form
        className="row wrap cp-inline-controls"
        onSubmit={(event) => {
          event.preventDefault();
          run(async () => {
            await api.createClientLogin(account.id, form);
            setForm({ username: '', password: '' });
          });
        }}
      >
        <input
          type="text"
          placeholder="New username"
          value={form.username}
          onChange={(event) => setForm({ ...form, username: event.target.value })}
          autoComplete="off"
          required
        />
        <input
          type="password"
          placeholder="Password"
          value={form.password}
          onChange={(event) => setForm({ ...form, password: event.target.value })}
          autoComplete="new-password"
          minLength={8}
          required
        />
        <button type="submit" className="tiny">
          Add login
        </button>
      </form>
      <div className="muted small">
        They sign in at this app's normal login page with the username (no email needed).
      </div>
    </div>
  );
}

const DETAIL_FIELDS = [
  ['full_name', 'Name'],
  ['current_title', 'Title'],
  ['current_company', 'Company'],
  ['location', 'Location'],
  ['linkedin_url', 'LinkedIn URL'],
  ['role_title', 'Shared for (role)'],
];

/** What the client sees about a person. Ours to correct; they read it as-is. */
function DetailsEditor({ detail, onSaved, onCancel }) {
  const [form, setForm] = useState(() =>
    Object.fromEntries(DETAIL_FIELDS.map(([key]) => [key, detail[key] ?? ''])),
  );
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function save(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSaved(await api.updateRecruiterSubmission(detail.id, form));
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack cp-details-form" onSubmit={save} data-testid="details-form">
      <ErrorBox error={error} />
      {DETAIL_FIELDS.map(([key, label]) => (
        <label key={key} className="cp-field-row">
          <span className="small muted">{label}</span>
          <input
            type="text"
            value={form[key]}
            onChange={(event) => setForm({ ...form, [key]: event.target.value })}
            required={key === 'full_name'}
            data-testid={`details-${key}`}
          />
        </label>
      ))}
      <div className="row">
        <button type="submit" className="primary" disabled={busy}>
          {busy ? 'Saving…' : 'Save details'}
        </button>
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function RecruiterExtras({ detail, applyUpdate, account, onRemoved }) {
  const [editing, setEditing] = useState(false);
  return (
    <section className="stack cp-recruiter-extras">
      <div className="sidebar-heading">Recruiter only</div>
      {editing ? (
        <DetailsEditor
          detail={detail}
          onSaved={(updated) => {
            applyUpdate(updated);
            setEditing(false);
          }}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <div className="row">
          <button type="button" onClick={() => setEditing(true)} data-testid="edit-details">
            Edit details
          </button>
          <button
            type="button"
            className="ghost danger"
            onClick={async () => {
              if (
                !window.confirm(
                  `Remove ${detail.full_name} from ${account.name}'s dashboard? Their notes and history go too.`,
                )
              )
                return;
              await api.unshareSubmission(detail.id);
              onRemoved(detail.id);
            }}
          >
            Remove from dashboard
          </button>
        </div>
      )}
    </section>
  );
}

function ClientTitle({ account, onRenamed }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(account.name);
  const [error, setError] = useState(null);

  if (!editing) {
    return (
      <span className="cp-title">
        {account.name}
        <button
          type="button"
          className="tiny ghost"
          onClick={() => {
            setName(account.name);
            setEditing(true);
          }}
          data-testid="rename-client"
        >
          Rename
        </button>
      </span>
    );
  }
  return (
    <form
      className="row cp-inline-controls"
      onSubmit={async (event) => {
        event.preventDefault();
        setError(null);
        try {
          onRenamed(await api.updateClient(account.id, { name: name.trim() }));
          setEditing(false);
        } catch (caught) {
          setError(caught);
        }
      }}
    >
      <input
        type="text"
        value={name}
        onChange={(event) => setName(event.target.value)}
        autoFocus
        required
        data-testid="rename-client-input"
      />
      <button type="submit" className="primary">
        Save
      </button>
      <button type="button" className="ghost" onClick={() => setEditing(false)}>
        Cancel
      </button>
      <ErrorBox error={error} />
    </form>
  );
}

export function ClientDetailPage() {
  const { clientId } = useParams();
  const [account, setAccount] = useState(null);
  const [error, setError] = useState(null);
  const [showShare, setShowShare] = useState(false);
  const [showLogins, setShowLogins] = useState(false);

  const loadAccount = useCallback(
    () => api.getClient(clientId).then(setAccount).catch(setError),
    [clientId],
  );
  useEffect(() => {
    loadAccount();
  }, [loadAccount]);

  const load = useCallback(() => api.clientSubmissionsFor(clientId), [clientId]);
  const { rows, setRows, error: rowsError, loading, syncedAt, refresh } = usePolledRows(load);

  // A dashboard with nobody on it has one thing to do next, so the share box
  // opens by itself rather than hiding behind a button.
  const empty = !loading && rows.length === 0;
  useEffect(() => {
    if (empty) setShowShare(true);
  }, [empty]);

  const pipelineApi = useMemo(
    () => ({
      loadDetail: (id) => api.recruiterSubmission(id),
      setStage: (id, stage) => api.updateRecruiterSubmission(id, { stage }),
      addNote: (id, body) => api.recruiterAddNote(id, body),
      deleteNote: (noteId) => api.recruiterDeleteNote(noteId),
      bulkStage: (ids, stage) => api.bulkClientStage(clientId, { ids, stage }),
      updateRole: (id, role_title) => api.updateRecruiterSubmission(id, { role_title }),
    }),
    [clientId],
  );

  if (error) return <ErrorBox error={error} />;
  if (!account) return <Loading />;

  const drawerExtras = (detail, applyUpdate) => (
    <RecruiterExtras
      detail={detail}
      applyUpdate={applyUpdate}
      account={account}
      onRemoved={(id) => {
        setRows(rows.filter((r) => r.id !== id));
        loadAccount();
      }}
    />
  );

  return (
    <div className="stack cp-ui">
      <PageHeader
        title={<ClientTitle account={account} onRenamed={setAccount} />}
        subtitle={
          <>
            <Link to="/clients">Clients</Link> · {account.total} shared
            {account.client_activity_7d
              ? ` · ${account.client_activity_7d} updates from ${account.name} this week`
              : ''}
            {syncedAt ? ` · synced ${new Date(syncedAt).toLocaleTimeString()}` : ''}
          </>
        }
        actions={
          <>
            <button type="button" className="tiny ghost" onClick={refresh}>
              Refresh
            </button>
            <button type="button" className="tiny ghost" onClick={() => setShowLogins((v) => !v)}>
              Logins ({account.logins.length})
            </button>
            <button type="button" className="primary" onClick={() => setShowShare((v) => !v)}>
              {showShare ? 'Hide sharing' : 'Share candidates'}
            </button>
          </>
        }
      />
      {showLogins ? <LoginsPanel account={account} onChange={loadAccount} /> : null}
      {showShare ? (
        <ShareForm
          accountId={account.id}
          onShared={() => {
            refresh();
            loadAccount();
          }}
        />
      ) : null}
      <ErrorBox error={rowsError} />
      {loading && !rows.length ? (
        <Loading />
      ) : (
        <ClientPipeline
          side="recruiter"
          rows={rows}
          api={pipelineApi}
          clientName={account.name}
          onRowsChange={setRows}
          drawerExtras={drawerExtras}
          exportName={`${account.name.toLowerCase()}-candidates`}
        />
      )}
    </div>
  );
}
