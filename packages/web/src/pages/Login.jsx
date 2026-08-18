import { useState } from 'react';

import { ErrorBox } from '../components/common.jsx';
import { useAuth } from '../lib/auth.jsx';

export function LoginPage() {
  const { login, signup } = useAuth();
  const [mode, setMode] = useState('login');
  const [form, setForm] = useState({
    email: 'demo@juicebox.dev',
    password: 'juicebox-demo',
    org_name: '',
    name: '',
  });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === 'login') {
        await login(form.email, form.password);
      } else {
        await signup({
          org_name: form.org_name || 'My Team',
          email: form.email,
          password: form.password,
          name: form.name,
        });
      }
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-shell">
      <form className="card auth-card stack" onSubmit={submit}>
        <div>
          <h1>
            Juice<span style={{ color: 'var(--accent)' }}>box</span>
          </h1>
          <p className="muted small">AI recruiting search and outreach.</p>
        </div>

        <div className="tabs">
          <button
            type="button"
            className={mode === 'login' ? 'primary' : 'ghost'}
            onClick={() => setMode('login')}
          >
            Log in
          </button>
          <button
            type="button"
            className={mode === 'signup' ? 'primary' : 'ghost'}
            onClick={() => setMode('signup')}
          >
            Create workspace
          </button>
        </div>

        <ErrorBox error={error} />

        {mode === 'signup' ? (
          <>
            <input
              type="text"
              placeholder="Company name"
              value={form.org_name}
              onChange={(event) => setForm({ ...form, org_name: event.target.value })}
            />
            <input
              type="text"
              placeholder="Your name"
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
            />
          </>
        ) : null}

        <input
          type="email"
          placeholder="Email"
          value={form.email}
          onChange={(event) => setForm({ ...form, email: event.target.value })}
          required
        />
        <input
          type="password"
          placeholder="Password"
          value={form.password}
          onChange={(event) => setForm({ ...form, password: event.target.value })}
          required
          minLength={8}
        />

        <button type="submit" className="primary" disabled={busy}>
          {busy ? 'Working…' : mode === 'login' ? 'Log in' : 'Create workspace'}
        </button>

        <p className="small muted" style={{ margin: 0 }}>
          Seeded demo account: <code className="mono">demo@juicebox.dev</code> /{' '}
          <code className="mono">juicebox-demo</code>
        </p>
      </form>
    </div>
  );
}
