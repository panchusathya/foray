import { useState } from 'react';

import { BrandMark, ErrorBox } from '../components/common.jsx';
import { useAuth } from '../lib/auth.jsx';

// The seeded demo account (seed.py) exists only on a dev database. Prefilling
// it in a production build greeted every visitor to app.goforay.io -- clients
// included -- with somebody's credentials that do not even work there.
const DEMO_PREFILL = import.meta.env.DEV
  ? { email: 'demo@goforay.io', password: 'foray-demo' }
  : { email: '', password: '' };

function EyeIcon({ open }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
      {open ? null : <path d="M3 3l18 18" />}
    </svg>
  );
}

export function LoginPage() {
  const { login, signup } = useAuth();
  const [mode, setMode] = useState('login');
  const [showPassword, setShowPassword] = useState(false);
  const [form, setForm] = useState({
    ...DEMO_PREFILL,
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
          <h1 className="brand-lockup">
            <BrandMark />
            <span>Foray</span>
          </h1>
          <p className="muted small">
            Foray into your <em className="marker">next hire</em>.
          </p>
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

        {/* Text, not email: a client company logs in with a username. */}
        <input
          type={mode === 'signup' ? 'email' : 'text'}
          placeholder={mode === 'signup' ? 'Email' : 'Email or username'}
          autoComplete="username"
          value={form.email}
          onChange={(event) => setForm({ ...form, email: event.target.value })}
          required
        />
        <div className="password-field">
          <input
            type={showPassword ? 'text' : 'password'}
            placeholder="Password"
            autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
            value={form.password}
            onChange={(event) => setForm({ ...form, password: event.target.value })}
            required
            minLength={8}
          />
          <button
            type="button"
            className="password-toggle"
            onClick={() => setShowPassword((v) => !v)}
            aria-label={showPassword ? 'Hide password' : 'Show password'}
            aria-pressed={showPassword}
            title={showPassword ? 'Hide password' : 'Show password'}
            data-testid="password-toggle"
          >
            <EyeIcon open={showPassword} />
          </button>
        </div>

        <button type="submit" className="primary" disabled={busy}>
          {busy ? 'Working…' : mode === 'login' ? 'Log in' : 'Create workspace'}
        </button>
      </form>
    </div>
  );
}
