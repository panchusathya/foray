/**
 * Google sign-in, rendered only when the deployment has a client id.
 *
 * This exists because the email door is not reliably open: outbound SMTP is
 * blocked at the network level on the production host, so a posted code may
 * never arrive. Signing in with Google sends no email at all.
 *
 * The GSI script is loaded on demand rather than in index.html — a portal
 * without Google configured should not be fetching Google's script on every
 * page load, and the CSP allowance is narrow enough that a stray load would
 * be the only third-party request the page makes.
 */

import { useEffect, useRef, useState } from 'react';

const GSI_SRC = 'https://accounts.google.com/gsi/client';

function loadGsi() {
  if (window.google?.accounts?.id) return Promise.resolve();
  const existing = document.querySelector(`script[src="${GSI_SRC}"]`);
  if (existing) {
    return new Promise((resolve, reject) => {
      existing.addEventListener('load', resolve, { once: true });
      existing.addEventListener('error', reject, { once: true });
    });
  }
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = GSI_SRC;
    script.async = true;
    script.onload = resolve;
    script.onerror = () => reject(new Error('Google sign-in failed to load'));
    document.head.appendChild(script);
  });
}

export function GoogleButton({ clientId, onCredential, onError }) {
  const holder = useRef(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!clientId) return undefined;
    let cancelled = false;

    loadGsi()
      .then(() => {
        if (cancelled || !holder.current) return;
        window.google.accounts.id.initialize({
          client_id: clientId,
          callback: (response) => onCredential(response.credential),
        });
        window.google.accounts.id.renderButton(holder.current, {
          theme: 'outline',
          size: 'large',
          width: 320,
          text: 'continue_with',
        });
        setReady(true);
      })
      .catch((err) => {
        // A blocked or failed script must not take the page down: the code
        // door is still there, and saying nothing beats an error the person
        // cannot act on.
        if (!cancelled && onError) onError(err);
      });

    return () => {
      cancelled = true;
    };
  }, [clientId, onCredential, onError]);

  if (!clientId) return null;
  return (
    <div className="google-wrap">
      <div ref={holder} />
      {!ready ? <div className="skel" style={{ height: 40 }} /> : null}
    </div>
  );
}
