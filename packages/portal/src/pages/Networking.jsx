/**
 * People worth talking to about an application you already have in flight.
 *
 * The tab the plan specified and the only page never built. Contacts are
 * per-application, because the useful question is not "who do I know" but
 * "who do I know at the place I just applied to" — so applications are the
 * spine here and contacts hang off whichever one you open.
 *
 * We never send these messages. The draft is written to be copied into
 * LinkedIn or an email by the candidate, in their own name; sending an
 * introduction on somebody's behalf to somebody who did not ask for it is a
 * different product, and a worse one.
 */

import { useCallback, useEffect, useState } from 'react';

import { Empty, ErrorNote, Screen, Skeleton } from '../components/ui.jsx';
import { api } from '../lib/api.js';

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard is permission-gated and blocked in some in-app browsers.
      // The draft is on screen either way, so this is a convenience, not the
      // feature — say nothing rather than throw an error at somebody.
      setCopied(false);
    }
  }, [text]);
  return (
    <button type="button" className="btn tiny" onClick={copy}>
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}

function Contact({ applicationId, contact }) {
  // refresh_networking caches a draft on the application, so one may
  // already be here — no reason to make somebody click for it again.
  const [draft, setDraft] = useState(contact.draft || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const write = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const data = await api.draftIntro(applicationId, contact.candidate_id);
      setDraft(data.draft || '');
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }, [applicationId, contact.candidate_id]);

  return (
    <div className="card stack-sm">
      <div className="row-between">
        <div>
          <div className="strong">{contact.name || 'Contact'}</div>
          <div className="muted small">
            {[contact.title, contact.company].filter(Boolean).join(' · ')}
          </div>
        </div>
        {contact.basis ? <span className="chip">{contact.basis}</span> : null}
      </div>

      <ErrorNote error={error} />

      {draft ? (
        <>
          <p className="draft">{draft}</p>
          <div className="row-between">
            <CopyButton text={draft} />
            {contact.linkedin_url ? (
              <a href={contact.linkedin_url} target="_blank" rel="noreferrer" className="btn tiny">
                Open LinkedIn
              </a>
            ) : null}
          </div>
        </>
      ) : (
        <button type="button" className="btn tiny" disabled={busy} onClick={write}>
          {busy ? 'Writing…' : 'Draft an intro'}
        </button>
      )}
    </div>
  );
}

function ApplicationContacts({ application }) {
  const [contacts, setContacts] = useState(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!open || contacts !== null) return;
    api
      .networking(application.id)
      .then((data) => setContacts(data.suggestions || []))
      .catch((err) => {
        setError(err);
        setContacts([]);
      });
  }, [open, contacts, application.id]);

  return (
    <div className="stack-sm">
      <button type="button" className="row-between disclosure" onClick={() => setOpen(!open)}>
        <span>
          <span className="strong">{application.company || 'Company'}</span>
          <span className="muted small"> · {application.title}</span>
        </span>
        <span className="muted">{open ? '−' : '+'}</span>
      </button>

      {open ? (
        <>
          <ErrorNote error={error} />
          {contacts === null ? <Skeleton count={2} height={70} /> : null}
          {contacts !== null && contacts.length === 0 ? (
            <p className="muted small">
              No one in our network there yet. This fills in as the team adds people.
            </p>
          ) : null}
          {(contacts || []).map((contact) => (
            <Contact key={contact.candidate_id} applicationId={application.id} contact={contact} />
          ))}
        </>
      ) : null}
    </div>
  );
}

export function NetworkingPage() {
  const [applications, setApplications] = useState(null);
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

  return (
    <Screen title="Networking" sub="Who to talk to where you've applied.">
      <ErrorNote error={error} />
      {applications === null ? <Skeleton count={3} /> : null}
      {applications !== null && applications.length === 0 ? (
        <Empty title="Nothing to network on yet">
          Apply to something first — then this shows who we know there.
        </Empty>
      ) : null}
      <div className="stack">
        {(applications || []).map((application) => (
          <ApplicationContacts key={application.id} application={application} />
        ))}
      </div>
    </Screen>
  );
}
