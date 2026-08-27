/**
 * Documents — every resume and cover letter we hold, tailored or uploaded.
 *
 * Split out of Profile: these are files, and files deserve their own page on
 * the rail. The tab bar drops it (five thumb targets is already the limit),
 * so Profile links here on a phone.
 */

import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { IconDoc, IconExternal } from '../components/icons.jsx';
import { BackLink, Empty, ErrorNote, Head, Screen, Skeleton } from '../components/ui.jsx';
import { api } from '../lib/api.js';
import { ago } from '../lib/applications.js';

const KINDS = {
  cover_letter: 'Cover letter',
  resume_uploaded: 'Resume, yours',
};

function labelFor(doc) {
  if (doc.kind === 'resume_generated') {
    return doc.application_id ? 'Resume, tailored' : 'Resume, ATS-ready';
  }
  return KINDS[doc.kind] || 'Document';
}

export function DocumentsPage() {
  const [documents, setDocuments] = useState(null);
  const [error, setError] = useState(null);
  const [preview, setPreview] = useState(null);
  const [vault, setVault] = useState([]);
  const [vaultNote, setVaultNote] = useState('');
  const [params] = useSearchParams();
  const askedVendor = params.get('vault') || '';
  const askedCompany = params.get('company') || '';
  const [credential, setCredential] = useState({
    vendor: askedVendor,
    tenant: params.get('tenant') || '',
    password: '',
  });

  const upload = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const doc = await api.uploadDocument(file);
      setDocuments((current) => [doc, ...(current || []).map((row) => ({ ...row, is_default: false, protected_default: false }))]);
    } catch (err) {
      setError(err);
    } finally {
      event.target.value = '';
    }
  };

  const makeDefault = async (doc) => {
    try {
      const updated = await api.makeDefaultDocument(doc.id);
      setDocuments((current) => (current || []).map((row) => row.id === doc.id ? updated : { ...row, is_default: false, protected_default: false }));
    } catch (err) {
      setError(err);
    }
  };

  const pw = credential.password;
  const passwordStrong = pw.length >= 10
    && /[a-z]/.test(pw) && /[A-Z]/.test(pw) && /[0-9]/.test(pw) && /[^A-Za-z0-9]/.test(pw);

  const saveCredential = async (event) => {
    event.preventDefault();
    try {
      await api.saveVaultAccount(credential);
      setCredential({ vendor: askedVendor, tenant: params.get('tenant') || '', password: '' });
      setVault(await api.vaultAccounts());
      setVaultNote(
        askedCompany
          ? `Saved. I'll set up your ${askedCompany} account and finish the application — you can close this.`
          : 'Credential saved.',
      );
    } catch (err) { setError(err); }
  };

  const revokeCredential = async (id) => {
    try {
      await api.revokeVaultAccount(id);
      setVault(await api.vaultAccounts());
      setVaultNote('Credential removed. Automated applications can no longer use it.');
    } catch (err) { setError(err); }
  };

  useEffect(() => {
    api
      .documents()
      .then((data) => setDocuments(data.documents || []))
      .catch((err) => {
        setError(err);
        setDocuments([]);
      });
  }, []);

  useEffect(() => {
    api.vaultAccounts().then(setVault).catch(() => setVault([]));
  }, []);

  const open = async (doc) => {
    try {
      const { url } = await api.documentUrl(doc.id);
      setPreview({ url, name: labelFor(doc) });
    } catch (err) {
      setError(err);
    }
  };

  const download = async (doc) => {
    try {
      // Always mint a fresh signed URL; a preview URL may have expired while
      // the candidate read the document.
      const { url } = await api.documentUrl(doc.id);
      window.location.assign(`${url}&download=true`);
    } catch (err) {
      setError(err);
    }
  };

  return (
    <Screen>
      <BackLink to="/profile">Profile</BackLink>
      <Head title="Documents" sub="Your resume, and every letter we wrote for you." />

      <label className="btn small" style={{ width: 'fit-content' }}>
        Upload a resume
        <input type="file" accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={upload} hidden />
      </label>

      <ErrorNote error={error} />

      {documents === null ? <Skeleton count={3} height={72} /> : null}

      {documents !== null && documents.length === 0 ? (
        <Empty title="No documents yet" glyph="+">
          Send your resume or LinkedIn to your agent and we write an ATS-ready resume; applying
          to a role adds a tailored one.
        </Empty>
      ) : null}

      <div className="stack tight">
        {(documents || []).map((doc) => (
          <div className="app-card" key={doc.id}>
            <span className="mono">
              <IconDoc style={{ width: 18, height: 18 }} />
            </span>
            <span className="grow">
              <span className="title truncate" style={{ display: 'block' }}>
                {labelFor(doc)}
              </span>
              <span className="meta">
                {doc.is_default ? 'Default · ' : ''}
                {doc.status === 'failed'
                  ? 'We hit a snag writing this one — it will retry automatically.'
                  : `${Math.max(1, Math.round((doc.size_bytes || 0) / 1024))} KB · ${ago(doc.updated_at)}`}
              </span>
            </span>
            {doc.status !== 'failed' ? (
              <span style={{ display: 'flex', gap: 'var(--s-2)' }}>
                <button type="button" className="btn small" onClick={() => open(doc)}>
                  View
                </button>
                <button type="button" className="btn small ghost" onClick={() => download(doc)}>
                  Download
                </button>
                {doc.kind === 'resume_uploaded' && !doc.is_default ? (
                  <button type="button" className="btn small ghost" onClick={() => makeDefault(doc)}>
                    Use this resume
                  </button>
                ) : null}
              </span>
            ) : null}
          </div>
        ))}
      </div>

      {preview ? (
        <section className="card" style={{ marginTop: 'var(--s-4)' }}>
          <div className="card-head">
            <h2>{preview.name}</h2>
            {/* iOS Safari renders PDFs in an iframe inconsistently, so the
                new-tab escape hatch sits next to the viewer rather than being
                a thing to discover after it fails. */}
            <a className="btn small ghost" href={preview.url} target="_blank" rel="noreferrer">
              New tab
              <IconExternal />
            </a>
          </div>
          <iframe className="viewer" src={preview.url} title={`${preview.name} preview`} />
        </section>
      ) : null}

      <section className="card stack" style={{ marginTop: 'var(--s-4)' }}>
        <div className="card-head">
          <h2>{askedCompany ? `Set your ${askedCompany} password` : 'Application account vault'}</h2>
        </div>
        <p className="tiny">
          {askedCompany
            ? `${askedCompany} makes you create an account to apply. Pick a password and I'll register it and finish your application. It's encrypted and never shown again.`
            : 'Store a job-board login for applications you approve. Your password is encrypted and never appears in messages or on this page.'}
        </p>
        {vault.map((account) => (
          <div className="spread" key={account.id}>
            <span className="tiny">{account.vendor} · {account.tenant} · {account.username} · {account.status}</span>
            {!account.revoked ? <button type="button" className="btn small ghost" onClick={() => revokeCredential(account.id)}>Remove</button> : null}
          </div>
        ))}
        <form className="stack tight" onSubmit={saveCredential}>
          {askedVendor ? null : (
            <>
              <input required value={credential.vendor} onChange={(event) => setCredential({ ...credential, vendor: event.target.value })} placeholder="Board (for example, workday)" aria-label="Board" />
              <input required value={credential.tenant} onChange={(event) => setCredential({ ...credential, tenant: event.target.value })} placeholder="Employer tenant" aria-label="Employer tenant" />
            </>
          )}
          <input required type="password" minLength="10" value={credential.password} onChange={(event) => setCredential({ ...credential, password: event.target.value })} placeholder="Password" aria-label="Password" />
          <p className="tiny">Use 10+ characters with an uppercase letter, a lowercase letter, a number, and a special character — the employer's site requires all of these.</p>
          <button type="submit" className="btn small" disabled={!passwordStrong}>{askedCompany ? 'Save and apply' : 'Save credential'}</button>
        </form>
        {vaultNote ? <p className="tiny">{vaultNote}</p> : null}
      </section>
    </Screen>
  );
}
