/**
 * The addresses on a person, editable.
 *
 * These were read-only until now, with a comment saying re-keying one "needs
 * its own deliberate flow". This is that flow, and what forced it was a
 * provider returning fabricated addresses: ContactOut answered HTTP 200 with a
 * sample profile once its quota ran out, and 30 records ended up carrying
 * email1@example.com and email2@gmail.com with no way to correct them from the
 * UI.
 *
 * **Order is not cosmetic.** The first address is Candidate.primary_email and is
 * what send_step sends to, so "Make primary" is a real action with a real
 * consequence, not a display preference. That is also why removing the first of
 * several asks for confirmation and the rest do not.
 *
 * Takes the whole list on every change, like TagEditor: no add/remove deltas, no
 * ordering problems, and re-saving the same list is a no-op.
 */

import { useState } from 'react';

import { api } from '../lib/api.js';
import { ErrorBox } from './common.jsx';

// Mirrors juicebox.enrich.normalize.is_placeholder. Duplicated deliberately and
// kept narrow: this only warns, and the server does not reject, because a human
// looking at a record is a better judge than a regex. mail@theirdomain.com is a
// real address; mail@gmail.com almost certainly is not.
const RESERVED = /@(?:example\.(?:com|net|org)|invalid|localhost|test)$|\.(?:example|invalid|localhost|test)$/i;
const FILLER = /^(?:e?-?mail|test|sample|user|username|name)[-_.]?\d{0,3}@|^(?:sample|example|your-?e?-?mail|first-?last|no-?reply|do-?not-?reply)@/i;

function looksFake(email) {
  return RESERVED.test(email) || FILLER.test(email);
}

function isAddress(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

export function EmailEditor({ candidateId, emails, onChanged }) {
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const current = emails ?? [];

  async function save(next) {
    setBusy(true);
    setError(null);
    try {
      await api.updateCandidate(candidateId, { emails: next });
      setDraft('');
      onChanged();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  function add() {
    const email = draft.trim().toLowerCase();
    if (!email) return undefined;
    if (!isAddress(email)) {
      setError(new Error(`"${email}" is not an email address.`));
      return undefined;
    }
    if (current.includes(email)) {
      setDraft('');
      return undefined;
    }
    return save([...current, email]);
  }

  function remove(email) {
    const primary = current[0] === email && current.length > 1;
    if (
      primary &&
      !window.confirm(
        `${email} is the address outreach sends to. Removing it promotes ` +
          `${current[1]} to primary.\n\nRemove it?`,
      )
    ) {
      return undefined;
    }
    return save(current.filter((one) => one !== email));
  }

  function promote(email) {
    return save([email, ...current.filter((one) => one !== email)]);
  }

  return (
    <div className="stack" data-testid="email-editor">
      {current.length === 0 ? (
        <span className="small muted">
          No address on file — they cannot be enrolled in a sequence.
        </span>
      ) : null}

      {current.map((email, index) => (
        <div key={email} className="row" style={{ gap: 8, alignItems: 'center' }}>
          <span className="mono">{email}</span>
          {index === 0 ? (
            <span className="badge" title="The address outreach sends to">
              primary
            </span>
          ) : (
            <button
              type="button"
              className="tiny ghost"
              disabled={busy}
              onClick={() => promote(email)}
            >
              Make primary
            </button>
          )}
          {looksFake(email) ? (
            <span className="small" title="This looks like placeholder data, not a real address">
              ⚠ looks fake
            </span>
          ) : null}
          <button
            type="button"
            className="tiny ghost"
            aria-label={`Remove ${email}`}
            disabled={busy}
            onClick={() => remove(email)}
          >
            ×
          </button>
        </div>
      ))}

      <div className="row" style={{ gap: 6 }}>
        <input
          value={draft}
          placeholder="add an address"
          disabled={busy}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              add();
            }
          }}
        />
        <button type="button" className="tiny" disabled={busy || !draft.trim()} onClick={add}>
          {busy ? 'Saving…' : 'Add'}
        </button>
      </div>

      {error ? <ErrorBox error={error} /> : null}
    </div>
  );
}
