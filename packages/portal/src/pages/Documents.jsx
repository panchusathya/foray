/**
 * Documents — every resume and cover letter we hold, tailored or uploaded.
 *
 * Split out of Profile: these are files, and files deserve their own page on
 * the rail. The tab bar drops it (five thumb targets is already the limit),
 * so Profile links here on a phone.
 */

import { useEffect, useState } from 'react';

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

  useEffect(() => {
    api
      .documents()
      .then((data) => setDocuments(data.documents || []))
      .catch((err) => {
        setError(err);
        setDocuments([]);
      });
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
    </Screen>
  );
}
