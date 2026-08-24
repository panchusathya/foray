import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { Empty, ErrorNote, Screen, Skeleton, StatusChip, statusLabel } from '../components/ui.jsx';
import { api } from '../lib/api.js';

const FILTERS = [
  { key: 'all', label: 'All', match: () => true },
  {
    key: 'review',
    label: 'Awaiting your OK',
    match: (a) => a.status === 'prepared' || a.status === 'awaiting_consent',
  },
  { key: 'sent', label: 'Sent', match: (a) => ['submitted', 'acknowledged'].includes(a.status) },
  {
    key: 'live',
    label: 'In process',
    match: (a) => ['interviewing', 'offer'].includes(a.status),
  },
  { key: 'closed', label: 'Closed', match: (a) => ['rejected', 'withdrawn'].includes(a.status) },
];

export function ApplicationsPage() {
  const [applications, setApplications] = useState(null);
  const [filter, setFilter] = useState('all');
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .applications()
      .then((data) => setApplications(data.applications || []))
      .catch((err) => {
        setError(err);
        setApplications([]);
      });
  }, []);

  const active = FILTERS.find((entry) => entry.key === filter) || FILTERS[0];
  const rows = (applications || []).filter(active.match);

  return (
    <Screen title="Applications" sub="Everything in flight, and what needs you.">
      <div className="tabs">
        {FILTERS.map((entry) => (
          <button
            key={entry.key}
            type="button"
            className={`tab ${entry.key === filter ? 'on' : ''}`}
            onClick={() => setFilter(entry.key)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <ErrorNote error={error} />
      {applications === null && <Skeleton />}
      {applications !== null && rows.length === 0 && (
        <Empty title="Nothing here yet">
          Applications appear once you swipe right on a role, or when your recruiter starts one
          for you.
        </Empty>
      )}

      <div className="stack" style={{ marginTop: 12 }}>
        {rows.map((application) => (
          <Link
            key={application.id}
            to={`/applications/${application.id}`}
            className="card"
            style={{ textDecoration: 'none' }}
          >
            <div className="spread">
              <div>
                <strong>{application.title || 'Role'}</strong>
                <div className="muted">{application.company}</div>
              </div>
              <StatusChip status={application.status} />
            </div>
          </Link>
        ))}
      </div>
    </Screen>
  );
}

const STEPS = ['prepared', 'awaiting_consent', 'submitted', 'acknowledged', 'interviewing'];

export function ApplicationDetailPage() {
  const { applicationId } = useParams();
  const [application, setApplication] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api
      .application(applicationId)
      .then(setApplication)
      .catch(setError);
  }, [applicationId]);

  useEffect(load, [load]);

  const act = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      setApplication(await fn(applicationId));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const openDocument = async (documentId) => {
    try {
      const { url } = await api.documentUrl(documentId);
      window.open(url, '_blank', 'noopener');
    } catch (err) {
      setError(err);
    }
  };

  if (!application) {
    return (
      <Screen title="Application">
        <ErrorNote error={error} />
        {!error && <Skeleton />}
      </Screen>
    );
  }

  const reached = STEPS.indexOf(application.status);
  const needsApproval = application.status === 'prepared';
  const readyToSend = application.status === 'awaiting_consent';

  return (
    <Screen title={application.title || 'Application'} sub={application.company}>
      <ErrorNote error={error} />

      <div className="card stack">
        <div className="spread">
          <StatusChip status={application.status} />
          {application.submission_url && (
            <a className="tiny" href={application.submission_url} target="_blank" rel="noreferrer">
              Job posting ↗
            </a>
          )}
        </div>

        <ul className="timeline">
          {STEPS.map((step, index) => (
            <li key={step} className={index <= reached ? 'done' : ''}>
              {statusLabel(step)}
            </li>
          ))}
        </ul>
      </div>

      {(application.documents || []).length > 0 && (
        <div className="card stack" style={{ marginTop: 12 }}>
          <strong>Your package</strong>
          {application.documents.map((doc) => (
            <div key={doc.id} className="spread">
              <span className="muted">
                {doc.kind === 'cover_letter' ? 'Cover letter' : 'Resume'}
                {doc.generated_by === 'llm' ? ' · tailored' : ''}
              </span>
              <button type="button" className="btn" onClick={() => openDocument(doc.id)}>
                View
              </button>
            </div>
          ))}
        </div>
      )}

      {(application.form_answers || []).length > 0 && (
        <div className="card stack" style={{ marginTop: 12 }}>
          <strong>Application answers</strong>
          {application.form_answers.map((answer) => (
            <div key={answer.key}>
              <div className="tiny">{answer.question}</div>
              <div>{answer.answer || <em className="muted">left blank for you to fill in</em>}</div>
            </div>
          ))}
        </div>
      )}

      <div className="stack" style={{ marginTop: 16 }}>
        {needsApproval && (
          <button
            type="button"
            className="btn primary wide"
            disabled={busy}
            onClick={() => act(api.approve)}
          >
            Approve this package
          </button>
        )}
        {readyToSend && (
          <>
            <p className="muted">
              Approved. Apply on the employer&apos;s site with the documents above, then mark it
              sent so your recruiter can track it.
            </p>
            <button
              type="button"
              className="btn primary wide"
              disabled={busy}
              onClick={() => act(api.confirmSubmitted)}
            >
              I&apos;ve applied
            </button>
          </>
        )}
        {!['rejected', 'withdrawn'].includes(application.status) && (
          <button
            type="button"
            className="btn wide"
            disabled={busy}
            onClick={() => act(api.withdraw)}
          >
            Withdraw
          </button>
        )}
      </div>

      <NetworkingSection applicationId={applicationId} />
    </Screen>
  );
}

function NetworkingSection({ applicationId }) {
  const [suggestions, setSuggestions] = useState(null);
  const [copied, setCopied] = useState('');

  useEffect(() => {
    api
      .networking(applicationId)
      .then((data) => setSuggestions(data.suggestions || []))
      .catch(() => setSuggestions([]));
  }, [applicationId]);

  const copy = async (contact) => {
    let text = contact.draft;
    if (!text) {
      const { draft } = await api.draftIntro(applicationId, contact.candidate_id);
      text = draft;
      setSuggestions((rows) =>
        rows.map((row) => (row.candidate_id === contact.candidate_id ? { ...row, draft } : row)),
      );
    }
    await navigator.clipboard.writeText(text);
    setCopied(contact.candidate_id);
  };

  if (!suggestions || suggestions.length === 0) return null;

  return (
    <div className="card stack" style={{ marginTop: 16 }}>
      <strong>People worth talking to</strong>
      <p className="tiny">
        Drafted for you to send yourself — nothing is sent on your behalf.
      </p>
      {suggestions.map((contact) => (
        <div key={contact.candidate_id} className="stack" style={{ gap: 6 }}>
          <div className="spread">
            <div>
              <strong>{contact.name}</strong>
              <div className="tiny">
                {contact.title ? `${contact.title} · ` : ''}
                {contact.basis === 'current' ? 'works there' : 'used to work there'}
              </div>
            </div>
            <button type="button" className="btn" onClick={() => copy(contact)}>
              {copied === contact.candidate_id ? 'Copied' : 'Copy intro'}
            </button>
          </div>
          {contact.draft && <p className="muted">{contact.draft}</p>}
        </div>
      ))}
    </div>
  );
}
