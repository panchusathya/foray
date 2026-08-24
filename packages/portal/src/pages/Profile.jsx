import { useEffect, useState } from 'react';

import { Empty, ErrorNote, Screen, Skeleton } from '../components/ui.jsx';
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

  if (!draft) return <Screen title="Profile"><Skeleton /></Screen>;

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

  return (
    <Screen title="Profile" sub="What your recruiter's agent knows about you.">
      <ErrorNote error={error} />
      {saved && <div className="banner info">Saved.</div>}

      <form className="card stack" onSubmit={save}>
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
        <button type="submit" className="btn primary wide" disabled={busy}>
          Save changes
        </button>
      </form>

      {preference && (
        <div className="card stack" style={{ marginTop: 12 }}>
          <strong>What you told us you want</strong>
          {preference.titles.length > 0 && (
            <div className="chips">
              {preference.titles.map((title) => (
                <span className="chip" key={title}>
                  {title}
                </span>
              ))}
            </div>
          )}
          {preference.locations.length > 0 && (
            <div className="chips">
              {preference.locations.map((place) => (
                <span className="chip" key={place}>
                  {place}
                </span>
              ))}
            </div>
          )}
          {preference.timeline && <div className="muted">Timeline: {preference.timeline}</div>}
          {preference.target_total ? (
            <div className="muted">
              Target: {preference.comp_currency} {Math.round(preference.target_total).toLocaleString()}
            </div>
          ) : null}
          <p className="tiny">To change any of this, just text your recruiter&apos;s agent.</p>
        </div>
      )}

      <button
        type="button"
        className="btn wide"
        style={{ marginTop: 16 }}
        onClick={signOut}
      >
        Sign out
      </button>
    </Screen>
  );
}

export function DocumentsPage() {
  const [documents, setDocuments] = useState(null);
  const [error, setError] = useState(null);
  const [preview, setPreview] = useState('');

  useEffect(() => {
    api
      .documents()
      .then((data) => setDocuments(data.documents || []))
      .catch((err) => {
        setError(err);
        setDocuments([]);
      });
  }, []);

  const open = async (documentId) => {
    try {
      const { url } = await api.documentUrl(documentId);
      setPreview(url);
    } catch (err) {
      setError(err);
    }
  };

  return (
    <Screen title="Documents" sub="Your resume and cover letters.">
      <ErrorNote error={error} />
      {documents === null && <Skeleton />}
      {documents !== null && documents.length === 0 && (
        <Empty title="No documents yet">
          Send your resume to your recruiter&apos;s agent, or apply to a role and one is written
          for you.
        </Empty>
      )}

      <div className="stack">
        {(documents || []).map((doc) => (
          <div className="card spread" key={doc.id}>
            <div>
              <strong>{labelFor(doc.kind)}</strong>
              <div className="tiny">
                {doc.is_default ? 'Default · ' : ''}
                {Math.max(1, Math.round((doc.size_bytes || 0) / 1024))} KB
              </div>
            </div>
            <button type="button" className="btn" onClick={() => open(doc.id)}>
              View
            </button>
          </div>
        ))}
      </div>

      {preview && (
        <div style={{ marginTop: 16 }}>
          {/* iOS Safari renders PDFs in an iframe inconsistently, so the
              new-tab escape hatch sits right next to it rather than being a
              thing to discover after it fails. */}
          <div className="spread" style={{ marginBottom: 8 }}>
            <span className="tiny">Preview</span>
            <a className="tiny" href={preview} target="_blank" rel="noreferrer">
              Open in a new tab ↗
            </a>
          </div>
          <iframe className="viewer" src={preview} title="Document preview" />
        </div>
      )}
    </Screen>
  );
}

function labelFor(kind) {
  if (kind === 'cover_letter') return 'Cover letter';
  if (kind === 'resume_generated') return 'Resume (tailored)';
  if (kind === 'resume_uploaded') return 'Resume (yours)';
  return 'Document';
}
