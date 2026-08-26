/**
 * The tracker, and one application in detail.
 *
 * Two views of the same list because the two questions are different: the
 * board answers "how is the search going" at a glance, the list answers "what
 * happened to the thing at Stripe". The chosen view is remembered, since
 * people have a strong preference and re-picking it on every visit is a small
 * daily insult.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import {
  IconArrow,
  IconBoard,
  IconBuilding,
  IconCheck,
  IconDoc,
  IconExternal,
  IconList,
  IconSearch,
  IconWrite,
} from '../components/icons.jsx';
import {
  BackLink,
  CopyButton,
  Empty,
  ErrorNote,
  Head,
  Mono,
  Note,
  Screen,
  Skeleton,
  StatusChip,
} from '../components/ui.jsx';
import { api } from '../lib/api.js';
import {
  CLOSED,
  IN_PROCESS,
  NEEDS_YOU,
  SENT,
  actionPrompt,
  ago,
  byRecency,
  groupByColumn,
  isClosed,
  matchesQuery,
  statusLabel,
  trackFor,
} from '../lib/applications.js';
import { basisLabel } from '../lib/network.js';
import { TrackerTabs } from './Inbox.jsx';
import { useApplications } from '../lib/store.jsx';

const VIEW_KEY = 'juicebox.portal.tracker-view';

const FILTERS = [
  { key: 'all', label: 'All', match: () => true },
  { key: 'needs', label: 'Needs you', match: (a) => NEEDS_YOU.includes(a.status) },
  { key: 'sent', label: 'Applied', match: (a) => SENT.includes(a.status) },
  { key: 'live', label: 'In process', match: (a) => IN_PROCESS.includes(a.status) },
  { key: 'closed', label: 'Closed', match: (a) => CLOSED.includes(a.status) },
];

function readView() {
  try {
    return window.localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'board';
  } catch {
    return 'board';
  }
}

export function ApplicationsPage() {
  const { applications, loading, error } = useApplications();
  const [view, setView] = useState(readView);
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');

  const choose = (next) => {
    setView(next);
    try {
      window.localStorage.setItem(VIEW_KEY, next);
    } catch {
      /* a remembered view is a nicety, not a feature */
    }
  };

  const rows = useMemo(() => byRecency(applications || []), [applications]);
  const searched = rows.filter((row) => matchesQuery(row, query));
  const active = FILTERS.find((entry) => entry.key === filter) || FILTERS[0];
  const listRows = searched.filter(active.match);

  return (
    <Screen wide>
      <Head
        title="Applications"
        sub="Everything in flight, and what it needs from you."
        action={
          <div className="head-actions">
            <TrackerTabs current="pipeline" />
            <div className="segmented" role="group" aria-label="View">
            <button
              type="button"
              className={view === 'board' ? 'on' : ''}
              onClick={() => choose('board')}
              aria-pressed={view === 'board'}
            >
              <IconBoard />
              Board
            </button>
            <button
              type="button"
              className={view === 'list' ? 'on' : ''}
              onClick={() => choose('list')}
              aria-pressed={view === 'list'}
            >
              <IconList />
              List
            </button>
            </div>
          </div>
        }
      />

      <label className="search">
        <IconSearch />
        <input
          type="search"
          value={query}
          placeholder="Search role, company or city"
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Search applications"
        />
      </label>

      <ErrorNote error={error} />

      {loading ? (
        <div style={{ marginTop: 'var(--s-4)' }}>
          <Skeleton count={4} height={68} />
        </div>
      ) : null}

      {!loading && rows.length === 0 ? (
        <div style={{ marginTop: 'var(--s-4)' }}>
          <Empty
            title="No applications yet"
            glyph="→"
            action={
              <Link className="btn primary" to="/discover">
                Start with a few roles
                <IconArrow />
              </Link>
            }
          >
            Swipe right on a role and we prepare the whole package — you approve it before
            anything is sent.
          </Empty>
        </div>
      ) : null}

      {!loading && rows.length > 0 && view === 'board' ? <Board applications={searched} /> : null}

      {!loading && rows.length > 0 && view === 'list' ? (
        <>
          <div className="filters" style={{ marginTop: 'var(--s-3)' }}>
            {FILTERS.map((entry) => {
              const count = searched.filter(entry.match).length;
              return (
                <button
                  key={entry.key}
                  type="button"
                  className={`filter${entry.key === filter ? ' on' : ''}`}
                  onClick={() => setFilter(entry.key)}
                  aria-pressed={entry.key === filter}
                >
                  {entry.label}
                  <span className="count">{count}</span>
                </button>
              );
            })}
          </div>

          {listRows.length === 0 ? (
            <Empty title="Nothing here">
              {query ? 'No application matches that search.' : 'Nothing sits in this stage today.'}
            </Empty>
          ) : (
            <div className="stack tight">
              {listRows.map((application) => (
                <Row key={application.id} application={application} />
              ))}
            </div>
          )}
        </>
      ) : null}
    </Screen>
  );
}

function Row({ application }) {
  return (
    <Link to={`/applications/${application.id}`} className="app-card">
      <Mono name={application.company} tone={NEEDS_YOU.includes(application.status) ? 'gold' : ''} />
      <span className="grow">
        <span className="title truncate" style={{ display: 'block' }}>
          {application.title || 'Role'}
        </span>
        <span className="meta truncate" style={{ display: 'block' }}>
          {application.company}
          {application.location ? ` · ${application.location}` : ''}
        </span>
      </span>
      <span className="stack tight" style={{ alignItems: 'flex-end', gap: 4 }}>
        <StatusChip status={application.status} />
        <span className="when">{ago(application.updated_at)}</span>
      </span>
    </Link>
  );
}

function Board({ applications }) {
  const columns = groupByColumn(applications);
  return (
    <div className="board">
      {columns.map((column) => (
        <section className="col" key={column.key} data-tone={column.tone}>
          <header className="col-head">
            <span className="dot" />
            <span className="name">{column.label}</span>
            <span className="n">{column.items.length}</span>
          </header>
          {column.items.length === 0 ? (
            <p className="col-empty">Nothing here</p>
          ) : (
            /* No monogram in a board card: a column narrow enough to fit six
               across leaves the title about eight characters once an avatar
               and its gap are taken out, and the title is the thing being
               scanned for. */
            column.items.map((application) => (
              <Link
                key={application.id}
                to={`/applications/${application.id}`}
                className="app-card"
              >
                <span className="co truncate">{application.company}</span>
                <span className="title">{application.title || 'Role'}</span>
                <span className="meta">{ago(application.updated_at)}</span>
              </Link>
            ))
          )}
        </section>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- detail --

function AnswersCard({ application, onSaved, locked }) {
  // Draft state per key, only for answers being edited right now.
  const [drafts, setDrafts] = useState({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const answers = application.form_answers || [];
  const attestations = answers.filter((a) => a.kind === 'attestation');
  const ordinary = answers.filter((a) => a.kind !== 'attestation');
  const dirty = Object.keys(drafts).filter(
    (key) => drafts[key] !== undefined
      && drafts[key] !== (answers.find((a) => a.key === key)?.answer || ''),
  );

  const save = async () => {
    if (!dirty.length || saving) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await api.saveAnswers(
        application.id,
        dirty.map((key) => ({ key, answer: drafts[key] })),
      );
      setDrafts({});
      onSaved(updated);
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  };

  const row = (answer) => {
    const value = drafts[answer.key] !== undefined ? drafts[answer.key] : answer.answer || '';
    return (
      <div className="qa" key={answer.key}>
        <div className="q">
          {answer.question}
          {answer.form_required ? <span className="req" title="The form requires this"> *</span> : null}
        </div>
        {locked ? (
          <div className="a">{answer.answer || <em className="muted">left blank</em>}</div>
        ) : (
          <textarea
            className="a-edit"
            rows={value.length > 120 ? 4 : 2}
            value={value}
            placeholder="Your answer, in your words…"
            onChange={(event) =>
              setDrafts((prev) => ({ ...prev, [answer.key]: event.target.value }))
            }
          />
        )}
      </div>
    );
  };

  return (
    <section className="card" style={{ marginTop: 'var(--s-3)' }}>
      <div className="card-head">
        <h2>Their questions, your answers</h2>
      </div>
      {ordinary.map(row)}
      {attestations.length ? (
        <>
          <div className="qa-divider">
            {/* These never come pre-filled and never will: they are the
                candidate's own statements. Blank is a real answer — it reads
                as declining to self-identify. */}
            <span className="tiny">
              Self-identification — optional, only you can answer these, and we never fill
              them in for you.
            </span>
          </div>
          {attestations.map(row)}
        </>
      ) : null}
      <ErrorNote error={error} />
      {!locked && dirty.length ? (
        <div className="qa-save">
          <button type="button" className="btn primary" disabled={saving} onClick={save}>
            {saving ? 'Saving…' : `Save ${dirty.length > 1 ? `${dirty.length} answers` : 'answer'}`}
          </button>
        </div>
      ) : null}
    </section>
  );
}

export function ApplicationDetailPage() {
  const { applicationId } = useParams();
  const { patch, reload } = useApplications();
  const [application, setApplication] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api.application(applicationId);
      setApplication(data);
      patch(data);
    } catch (err) {
      setError(err);
    }
    // `patch` is stable for the life of the provider; listing it would re-run
    // this on every list change, which is exactly what we are avoiding.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applicationId]);

  useEffect(() => {
    load();
  }, [load]);

  const act = async (fn, { rereads = false } = {}) => {
    setBusy(true);
    setError(null);
    try {
      const result = await fn(applicationId);
      // Some actions answer with a queued job rather than an application, and
      // a job id rendered as an application is a blank screen. Re-read those.
      if (rereads) await load();
      else {
        setApplication(result);
        patch(result);
      }
      reload();
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
      <Screen>
        <BackLink to="/applications">Applications</BackLink>
        <ErrorNote error={error} />
        {!error ? <Skeleton count={2} height={110} /> : null}
      </Screen>
    );
  }

  const track = trackFor(application.status);
  const needsApproval = application.status === 'prepared';
  const readyToSend = application.status === 'awaiting_consent';
  const inFlight = application.status === 'submitting';
  // The server decides whether we have an adapter for this employer's ATS.
  // Offering a button that answers 409 is worse than offering none.
  const weCanSend = Boolean(application.can_auto_submit);

  return (
    <Screen>
      <BackLink to="/applications">Applications</BackLink>

      <header className="detail-head">
        <Mono name={application.company} size="lg" tone={needsApproval || readyToSend ? 'gold' : ''} />
        <div className="grow">
          <h1>{application.title || 'Application'}</h1>
          <div className="where">
            {application.company}
            {application.location ? ` · ${application.location}` : ''}
          </div>
        </div>
      </header>

      <ErrorNote error={error} />

      <div className="card">
        <div className="spread">
          <StatusChip status={application.status} />
          {application.submission_url ? (
            <a
              className="btn small ghost"
              href={application.submission_url}
              target="_blank"
              rel="noreferrer"
            >
              Job posting
              <IconExternal />
            </a>
          ) : null}
        </div>

        {track ? (
          <div className="track">
            {track.map((step) => (
              <div key={step.key} className={`step ${step.state}`}>
                <div className="bar" />
                <div className="lbl">{step.label}</div>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted" style={{ marginTop: 'var(--s-3)' }}>
            {statusLabel(application.status)}
            {application.status === 'withdrawn'
              ? ' — you pulled this one.'
              : ' — this one is not moving forward.'}
          </p>
        )}

        <p className="tiny">Updated {ago(application.updated_at)}</p>
      </div>

      {inFlight ? (
        <div style={{ marginTop: 'var(--s-3)' }}>
          <Note>
            Sending this now. We&apos;ll message you the moment it lands, or if their form asks
            for something we don&apos;t have.
          </Note>
        </div>
      ) : null}

      {(application.documents || []).length > 0 ? (
        <section className="card" style={{ marginTop: 'var(--s-3)' }}>
          <div className="card-head">
            <h2>Your package</h2>
            <span className="tiny">Written for this role</span>
          </div>
          <div className="stack tight">
            {application.documents.map((doc) => (
              <div key={doc.id} className="doc-row">
                <span className="mono sm">
                  <IconDoc style={{ width: 16, height: 16 }} />
                </span>
                <span className="grow">
                  <span className="strong" style={{ display: 'block', fontSize: 14 }}>
                    {doc.kind === 'cover_letter' ? 'Cover letter' : 'Resume'}
                  </span>
                  <span className="tiny">
                    {doc.generated_by === 'llm' ? 'Tailored for this application' : 'Your upload'}
                  </span>
                </span>
                <button type="button" className="btn small" onClick={() => openDocument(doc.id)}>
                  View
                </button>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {(application.form_answers || []).length > 0 ? (
        <AnswersCard
          application={application}
          onSaved={setApplication}
          locked={!['draft', 'prepared', 'awaiting_consent'].includes(application.status)}
        />
      ) : null}

      <ApplicationPeople applicationId={applicationId} company={application.company} />

      <div className="actionbar">
        {needsApproval ? (
          <>
            <p className="tiny">{actionPrompt(application)}</p>
            <button
              type="button"
              className="btn primary wide"
              disabled={busy}
              onClick={() => act(api.approve)}
            >
              <IconCheck />
              Approve this package
            </button>
          </>
        ) : null}

        {readyToSend && weCanSend ? (
          <>
            <p className="tiny">
              Approved. We can send this one for you, or you can apply yourself with the documents
              above.
            </p>
            <button
              type="button"
              className="btn primary wide"
              disabled={busy}
              onClick={() => act(api.submitForMe, { rereads: true })}
            >
              Apply for me
            </button>
            <button
              type="button"
              className="btn wide"
              disabled={busy}
              onClick={() => act(api.confirmSubmitted)}
            >
              I applied myself
            </button>
          </>
        ) : null}

        {readyToSend && !weCanSend ? (
          <>
            <p className="tiny">{actionPrompt(application)}</p>
            {application.submission_url ? (
              <a
                className="btn primary wide"
                href={application.submission_url}
                target="_blank"
                rel="noreferrer"
              >
                Open the application
                <IconExternal />
              </a>
            ) : null}
            <button
              type="button"
              className="btn wide"
              disabled={busy}
              onClick={() => act(api.confirmSubmitted)}
            >
              I applied
            </button>
          </>
        ) : null}

        {!isClosed(application.status) ? (
          <button
            type="button"
            className="btn ghost wide"
            disabled={busy}
            onClick={() => act(api.withdraw)}
          >
            Withdraw this application
          </button>
        ) : null}
      </div>
    </Screen>
  );
}

/**
 * Who we know at this company, and the message we wrote them.
 *
 * Nothing here is ever sent for the candidate. A drafted introduction sent on
 * somebody's behalf to somebody who did not ask for it is a different product,
 * and a worse one — so the only actions are copy and open.
 */
function ApplicationPeople({ applicationId, company }) {
  const [contacts, setContacts] = useState(null);
  const [drafting, setDrafting] = useState('');

  useEffect(() => {
    let cancelled = false;
    api
      .networking(applicationId)
      .then((data) => !cancelled && setContacts(data.suggestions || []))
      .catch(() => !cancelled && setContacts([]));
    return () => {
      cancelled = true;
    };
  }, [applicationId]);

  const write = async (contact) => {
    setDrafting(contact.candidate_id);
    try {
      const { draft } = await api.draftIntro(applicationId, contact.candidate_id);
      setContacts((rows) =>
        rows.map((row) => (row.candidate_id === contact.candidate_id ? { ...row, draft } : row)),
      );
    } catch {
      /* the button simply stays available */
    } finally {
      setDrafting('');
    }
  };

  if (!contacts || contacts.length === 0) return null;

  return (
    <section className="card" style={{ marginTop: 'var(--s-3)' }}>
      <div className="card-head">
        <h2>People at {company || 'this company'}</h2>
        <Link to="/network" className="section-link">
          All people
          <IconArrow />
        </Link>
      </div>
      <p className="tiny" style={{ marginBottom: 'var(--s-3)' }}>
        Drafted for you to send in your own name. We never send these for you.
      </p>
      <div className="stack tight">
        {contacts.map((contact) => (
          <div key={contact.candidate_id} className="doc-row" style={{ alignItems: 'flex-start' }}>
            <Mono name={contact.name} size="sm" />
            <div className="grow stack tight">
              <div>
                <div className="strong" style={{ fontSize: 14 }}>
                  {contact.name}
                </div>
                <div className="tiny">
                  {[contact.title, basisLabel(contact.basis)].filter(Boolean).join(' · ')}
                </div>
              </div>
              {contact.draft ? <p className="draft open">{contact.draft}</p> : null}
              <div className="btn-row">
                {contact.draft ? (
                  <CopyButton text={contact.draft} label="Copy message" />
                ) : (
                  <button
                    type="button"
                    className="btn small"
                    disabled={drafting === contact.candidate_id}
                    onClick={() => write(contact)}
                  >
                    <IconWrite />
                    {drafting === contact.candidate_id ? 'Writing…' : 'Draft a message'}
                  </button>
                )}
                {contact.linkedin_url ? (
                  <a
                    className="btn small ghost"
                    href={contact.linkedin_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <IconBuilding />
                    LinkedIn
                  </a>
                ) : null}
              </div>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
