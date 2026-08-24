import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { GoogleButton } from '../components/GoogleButton.jsx';
import { ErrorNote, Screen } from '../components/ui.jsx';
import { api } from '../lib/api.js';
import { once } from '../lib/once.js';
import { useAuth } from '../lib/auth.jsx';

// The magic-link landing. Loading this page is a plain GET with no side
// effects -- an iMessage link preview prefetches it, and burning the grant on
// a prefetch would mean the person's own tap always failed. The exchange is
// the POST below.
export function MagicLinkPage() {
  const { token } = useParams();
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    // once(): the grant is single-use, so the request itself is deduped, not
    // just the state update. StrictMode's second mount would otherwise spend
    // the grant on a request whose result nobody reads, and the person's own
    // tap would report an expired link. See lib/once.js.
    once(token, () => api.exchange(token))
      .then(async (result) => {
        if (cancelled) return;
        await signIn(result.access_token);
        navigate(result.next || '/', { replace: true });
      })
      .catch((err) => !cancelled && setError(err));
    return () => {
      cancelled = true;
    };
  }, [token, signIn, navigate]);

  if (!error) {
    return (
      <Screen title="Signing you in…">
        <div className="skel" style={{ height: 120 }} />
      </Screen>
    );
  }

  return (
    <Screen title="That link has expired">
      <p className="muted">
        Links are single-use and short-lived. Text your recruiter&apos;s agent for a fresh one, or
        sign in with a code.
      </p>
      <button type="button" className="btn primary wide" onClick={() => navigate('/login')}>
        Email me a code
      </button>
    </Screen>
  );
}

export function LoginPage() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [stage, setStage] = useState('email');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [googleClientId, setGoogleClientId] = useState('');

  // Ask the server which doors are open rather than hardcoding: a deployment
  // with no Google client configured should not show a button that cannot work.
  useEffect(() => {
    api
      .authConfig()
      .then((data) => setGoogleClientId(data.google_client_id || ''))
      .catch(() => setGoogleClientId(''));
  }, []);

  const onGoogle = useCallback(
    async (credential) => {
      setBusy(true);
      setError(null);
      try {
        const result = await api.googleSignIn(credential);
        await signIn(result.access_token);
        navigate('/', { replace: true });
      } catch (err) {
        setError(err);
      } finally {
        setBusy(false);
      }
    },
    [signIn, navigate],
  );

  const requestCode = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.requestCode(email);
      setStage('code');
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const verify = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.verifyCode(email, code);
      await signIn(result.access_token);
      navigate(result.next || '/', { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Screen title="Sign in" sub="No password — we'll email you a code.">
      <ErrorNote error={error} />
      {googleClientId && stage === 'email' ? (
        <div className="card stack">
          <GoogleButton clientId={googleClientId} onCredential={onGoogle} />
          <div className="or">or</div>
        </div>
      ) : null}
      {stage === 'email' ? (
        <form className="card stack" onSubmit={requestCode}>
          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              inputMode="email"
              autoComplete="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </div>
          <button type="submit" className="btn primary wide" disabled={busy}>
            Send me a code
          </button>
        </form>
      ) : (
        <form className="card stack" onSubmit={verify}>
          <p className="muted">
            If we know that address, a six-digit code is on its way to it.
          </p>
          <div className="field">
            <label htmlFor="code">Code</label>
            <input
              id="code"
              inputMode="numeric"
              /* iOS lifts the code straight out of Mail with this. */
              autoComplete="one-time-code"
              pattern="[0-9]*"
              maxLength={6}
              required
              value={code}
              onChange={(event) => setCode(event.target.value)}
            />
          </div>
          <button type="submit" className="btn primary wide" disabled={busy}>
            Sign in
          </button>
          <button type="button" className="btn wide" onClick={() => setStage('email')}>
            Use a different email
          </button>
        </form>
      )}
    </Screen>
  );
}
