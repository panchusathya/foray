/**
 * Profile — what the agent knows, and the only part of it the candidate owns.
 *
 * The editable set is a strict subset of the recruiter's (PortalProfilePatch
 * on the server); everything else on this page is read-only on purpose, and
 * says how to change it. Preferences are shown but not edited here — they are
 * settled in conversation with the agent, and a form that silently disagreed
 * with what somebody said out loud would be worse than no form.
 */

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { IconArrow, IconDoc, IconSignOut } from '../components/icons.jsx';
import { ErrorNote, Head, Mono, Note, Screen, Skeleton } from '../components/ui.jsx';
import { api } from '../lib/api.js';
import { useAuth } from '../lib/auth.jsx';

const EDITABLE = [
  ['first_name', 'First name'],
  ['last_name', 'Last name'],
  ['headline', 'Headline'],
  ['location_city', 'City'],
  ['location_region', 'State / region'],
  ['linkedin_url', 'LinkedIn'],
];

export function ProfilePage() {
  const { profile, reload, signOut } = useAuth();
  const [draft, setDraft] = useState(null);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (profile) setDraft({ ...profile.candidate });
  }, [profile]);

  if (!draft) {
    return (
      <Screen>
        <Head title="Profile" />
        <Skeleton count={2} height={140} />
      </Screen>
    );
  }

  const save = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const fields = Object.fromEntries(EDITABLE.map(([key]) => [key, draft[key] || '']));
      await api.patchProfile(fields);
      await reload();
      setSaved(true);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const preference = profile.preference;
  const name = draft.full_name || `${draft.first_name || ''} ${draft.last_name || ''}`.trim();
  const email = (draft.emails || [])[0] || '';

  return (
    <Screen>
      <Head title="You" sub="What your agent knows, and what it looks for." />

      <ErrorNote error={error} />
      {saved ? <Note tone="good">Saved.</Note> : null}

      <div className="card" style={{ marginTop: saved ? 'var(--s-3)' : 0 }}>
        <div className="row">
          <Mono name={name || email || '?'} size="lg" />
          <div className="grow">
            <div className="strong truncate">{name || 'Your profile'}</div>
            <div className="tiny truncate">{draft.headline || email}</div>
          </div>
        </div>
      </div>

      <Link className="app-card" to="/documents" style={{ marginTop: 'var(--s-3)' }}>
        <span className="mono">
          <IconDoc style={{ width: 18, height: 18 }} />
        </span>
        <span className="grow">
          <span className="title" style={{ display: 'block' }}>
            Documents
          </span>
          <span className="meta">Your resume and every letter we wrote</span>
        </span>
        <IconArrow style={{ width: 17, height: 17, color: 'var(--faint)' }} />
      </Link>

      <form className="card stack" onSubmit={save} style={{ marginTop: 'var(--s-3)' }}>
        <div className="card-head">
          <h2>Your details</h2>
        </div>
        <div className="grid-2">
          {EDITABLE.map(([key, label]) => (
            <div className="field" key={key}>
              <label htmlFor={key}>{label}</label>
              <input
                id={key}
                value={draft[key] || ''}
                onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
              />
            </div>
          ))}
        </div>
        <button type="submit" className="btn primary wide" disabled={busy}>
          {busy ? 'Saving…' : 'Save changes'}
        </button>
      </form>

      {preference ? (
        <section className="card" style={{ marginTop: 'var(--s-3)' }}>
          <div className="card-head">
            <h2>What you told us you want</h2>
          </div>
          <div className="stack tight">
            {preference.titles.length > 0 ? (
              <div>
                <div className="tiny" style={{ marginBottom: 6 }}>
                  Roles
                </div>
                <div className="chips">
                  {preference.titles.map((title) => (
                    <span className="chip accent" key={title}>
                      {title}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}

            {preference.locations.length > 0 ? (
              <div>
                <div className="tiny" style={{ marginBottom: 6 }}>
                  Places
                </div>
                <div className="chips">
                  {preference.locations.map((place) => (
                    <span className="chip" key={place}>
                      {place}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}

            {preference.timeline ? (
              <div className="spread">
                <span className="tiny">Timeline</span>
                <span className="strong" style={{ fontSize: 14 }}>
                  {preference.timeline}
                </span>
              </div>
            ) : null}

            {preference.target_total ? (
              <div className="spread">
                <span className="tiny">Target</span>
                <span className="strong" style={{ fontSize: 14 }}>
                  {preference.comp_currency}{' '}
                  {Math.round(preference.target_total).toLocaleString()}
                </span>
              </div>
            ) : null}
          </div>
          <p className="tiny" style={{ marginTop: 'var(--s-3)' }}>
            To change any of this, just message your agent — it updates here.
          </p>
        </section>
      ) : null}

      <button type="button" className="btn wide" style={{ marginTop: 'var(--s-5)' }} onClick={signOut}>
        <IconSignOut />
        Sign out
      </button>
    </Screen>
  );
}
