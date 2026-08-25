/**
 * The numbers on a person, editable. The sibling of ``EmailEditor``.
 *
 * Intake is what made this worth building: a call and a text conversation both
 * arrive with a number attached, and until now the CRM stored it and showed it
 * nowhere, so the number a candidate had just been texting from was invisible
 * to the recruiter following up.
 *
 * **Order matters less here than it does for addresses, but it is not
 * nothing.** Nothing dials automatically, so the first number is not a send
 * target the way ``emails[0]`` is; it is the one every compact surface shows.
 * Hence "Make primary" without the confirmation prompt removing a primary
 * address needs.
 *
 * The warning is about lookups, not deliverability. ``intake.phones.lookup_key``
 * refuses to guess a country code -- a wrong email lookup finds nobody, a wrong
 * *phone* lookup finds the wrong person and reads their pipeline to whoever
 * dialled -- so a number stored without one is real data the dial-in line
 * simply cannot match. Saying so is more useful than rejecting it, because the
 * digits are still what a human needs in order to ring them.
 *
 * Takes the whole list on every change, like ``EmailEditor`` and ``TagEditor``:
 * no add/remove deltas, no ordering problems, re-saving is a no-op.
 */

import { useState } from 'react';

import { api } from '../lib/api.js';
import { formatPhone, telHref } from '../lib/phone.js';
import { ErrorBox } from './common.jsx';

// Mirrors juicebox.enrich.normalize.clean_phones: seven digits or more, once
// everything that is not a digit or a leading + is stripped.
function isNumber(value) {
  return value.replace(/\D/g, '').length >= 7;
}

// Mirrors juicebox.intake.phones.lookup_key's admission rule, narrowly: E.164,
// or ten/eleven digits that are North-American-shaped. Anything else is stored
// but not matchable.
function isMatchable(value) {
  const raw = value.replace(/[^\d+]/g, '');
  if (raw.startsWith('+')) return raw.length >= 11;
  const digits = raw.replace(/\D/g, '');
  return digits.length === 10 || (digits.length === 11 && digits.startsWith('1'));
}

export function PhoneEditor({ candidateId, phones, onChanged }) {
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const current = phones ?? [];

  async function save(next) {
    setBusy(true);
    setError(null);
    try {
      await api.updateCandidate(candidateId, { phones: next });
      setDraft('');
      onChanged();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  function add() {
    const value = draft.trim();
    if (!value) return undefined;
    if (!isNumber(value)) {
      setError(new Error(`"${value}" does not have enough digits to be a phone number.`));
      return undefined;
    }
    // Compared on the normalised form: "(415) 555-0123" and "+1 415 555 0123"
    // are the same number, and the server stores one of them.
    const normalized = value.replace(/[^\d+]/g, '');
    if (current.some((one) => one.replace(/[^\d+]/g, '') === normalized)) {
      setDraft('');
      return undefined;
    }
    return save([...current, value]);
  }

  function remove(value) {
    return save(current.filter((one) => one !== value));
  }

  function promote(value) {
    return save([value, ...current.filter((one) => one !== value)]);
  }

  return (
    <div className="stack" data-testid="phone-editor">
      {current.length === 0 ? (
        <span className="small muted">No number on file.</span>
      ) : null}

      {current.map((phone, index) => (
        <div key={phone} className="row" style={{ gap: 8, alignItems: 'center' }}>
          <a className="mono" href={telHref(phone)}>
            {formatPhone(phone)}
          </a>
          {index === 0 ? (
            <span className="badge" title="Shown wherever there is room for one number">
              primary
            </span>
          ) : (
            <button
              type="button"
              className="tiny ghost"
              disabled={busy}
              onClick={() => promote(phone)}
            >
              Make primary
            </button>
          )}
          {isMatchable(phone) ? null : (
            <span
              className="small"
              title="No country code, so an inbound call from this number cannot be matched to this person"
            >
              ⚠ not matchable
            </span>
          )}
          <button
            type="button"
            className="tiny ghost"
            aria-label={`Remove ${phone}`}
            disabled={busy}
            onClick={() => remove(phone)}
          >
            ×
          </button>
        </div>
      ))}

      <div className="row" style={{ gap: 6 }}>
        <input
          value={draft}
          placeholder="+1 415 555 0123"
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
