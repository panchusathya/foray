/**
 * What we are putting in people's names, and where each one stands.
 *
 * The candidate drives most of this from the portal — they swipe, they
 * approve a package, they mark it sent. This page is the recruiter's window
 * on the same rows, plus the moves only a human on our side can make:
 * recording that an employer acknowledged, moved to interview, or passed.
 * Nothing here reaches into the candidate's inbox, because their mailbox is
 * not connected; a status only changes when somebody tells us.
 *
 * Two things it deliberately shows that the candidate's own view does not:
 * the provenance behind every generated answer, and the consent trail. If we
 * asserted something under somebody's name, a recruiter should be able to see
 * where it came from without opening a database.
 */

import { useCallback, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading, PageHeader, ViewTabs } from '../components/common.jsx';
import {
  APPLICATION_STATUS_LABELS,
  APPLICATION_TRANSITIONS,
  api,
  applicationStatusTone,
} from '../lib/api.js';
import { APPLICATION_VIEWS, applicationView, inView } from '../lib/applicationViews.js';
import { useAsync } from '../lib/useAsync.js';

export function statusLabel(status) {
  return APPLICATION_STATUS_LABELS[status] ?? status;
}

export function ApplicationStatus({ status }) {
  return <Badge tone={applicationStatusTone(status)}>{statusLabel(status)}</Badge>;
}

function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString();
}

export function ApplicationsPage() {
  const [params] = useSearchParams();
  const view = applicationView(params.get('view') ?? 'open');
  const list = useAsync(() => api.listApplications(), []);
  const rows = (list.data ?? []).filter((row) => inView(view, row));

  return (
    <div className="stack">
      <PageHeader
        title="Applications"
        subtitle="Every pursuit we have prepared or sent on a candidate's behalf."
      />
      <ViewTabs views={APPLICATION_VIEWS} activeId={view.id} basePath="/applications" />
      <p className="muted small">{view.blurb}</p>
      <ErrorBox error={list.error} />

      {list.loading ? <Loading /> : null}
      {!list.loading && rows.length === 0 ? (
        <Empty>
          Nothing here. Applications appear when a candidate swipes right in the portal, says
          &ldquo;apply&rdquo; in a thread, or when you start one for them.
        </Empty>
      ) : null}

      {rows.length > 0 ? (
        <table className="jobs-table" data-testid="applications-table">
          <thead>
            <tr>
              <th>Candidate</th>
              <th>Role</th>
              <th>Status</th>
              <th>Started by</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>
                  <Link to={`/applications/${row.id}`}>{row.candidate_name || 'Candidate'}</Link>
                </td>
                <td>
                  {/* The role is what people scan this table for, so it opens
                      the application; the posting itself is one hop further
                      out and gets its own link when we have the URL. */}
                  <Link to={`/applications/${row.id}`}>{row.title}</Link>
                  <div className="muted small">
                    {row.company}
                    {row.posting_url || row.submission_url ? (
                      <>
                        {' · '}
                        <a
                          href={row.posting_url || row.submission_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Job posting ↗
                        </a>
                      </>
                    ) : null}
                  </div>
                </td>
                <td>
                  <ApplicationStatus status={row.status} />
                </td>
                <td className="muted small">{row.source}</td>
                <td className="muted small">{formatDate(row.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

export function ApplicationDetailPage() {
  const { applicationId } = useParams();
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const detail = useAsync(() => api.getApplication(applicationId), [applicationId]);
  const attempts = useAsync(
    () => api.applicationSubmissions(applicationId),
    [applicationId],
  );
  const application = detail.data;

  const act = useCallback(
    async (fn) => {
      setBusy(true);
      setError(null);
      try {
        await fn();
        await detail.reload();
        await attempts.reload();
      } catch (err) {
        setError(err);
      } finally {
        setBusy(false);
      }
    },
    [detail, attempts],
  );

  if (detail.loading) return <Loading />;
  if (!application) {
    return (
      <div className="stack">
        <ErrorBox error={detail.error} />
        <Empty>Application not found.</Empty>
      </div>
    );
  }

  const consented = Boolean(application.consenting_message_id || application.approved_at);
  // The transition table says prepared -> submitted is legal; state.py adds
  // that it is legal *with a recorded yes*. Offering the button anyway means
  // a recruiter clicks it and reads a 409 — so the UI mirrors the real rule,
  // and the consent line above says what is missing.
  const moves = (APPLICATION_TRANSITIONS[application.status] ?? []).filter(
    (next) => next !== 'submitted' || consented,
  );

  return (
    <div className="stack">
      <PageHeader
        title={application.title || 'Application'}
        subtitle={[application.company, application.candidate_name].filter(Boolean).join(' — ')}
        actions={
          <Link to={`/contacts/${application.candidate_id}`}>Open contact</Link>
        }
      />
      <ErrorBox error={error ?? detail.error} />

      <div className="card stack">
        <div className="spread">
          <ApplicationStatus status={application.status} />
          {application.posting_url || application.submission_url ? (
            <a
              href={application.posting_url || application.submission_url}
              target="_blank"
              rel="noreferrer"
            >
              Job posting ↗
            </a>
          ) : null}
        </div>

        {/* The consent trail. An application cannot reach "submitted" without
            one -- state.py refuses the transition -- so this reads as
            reassurance on a sent row, and as the outstanding blocker on one
            that has not gone yet. */}
        <div className="muted small" data-testid="application-consent">
          {consented
            ? `Consent on file${
                application.consent_verbatim ? `: “${application.consent_verbatim}”` : ''
              }${application.approved_at ? ' (approved in the portal)' : ''}`
            : 'No consent recorded yet — this cannot be submitted until the candidate says yes.'}
        </div>

        {application.package_error ? (
          <div className="error">Package build failed: {application.package_error}</div>
        ) : null}
      </div>

      {['prepared', 'awaiting_consent'].includes(application.status) && consented ? (
        <div className="card stack">
          <h3>Send it</h3>
          <p className="muted small">
            Queues one attempt. Most postings have no adapter for their ATS and stay a
            hand-off — the refusal says which.
          </p>
          <div>
            <button
              type="button"
              className="tiny"
              disabled={busy}
              onClick={() => act(() => api.submitApplication(application.id))}
            >
              Submit now
            </button>
          </div>
        </div>
      ) : null}

      <SubmissionAttempts attempts={attempts} />

      <div className="card stack">
        <h3>Package</h3>
        {application.documents?.length ? (
          application.documents.map((doc) => (
            <div className="spread" key={doc.id}>
              <span>
                {doc.kind === 'cover_letter' ? 'Cover letter' : 'Resume'}
                <span className="muted small">
                  {' '}
                  · {doc.generated_by === 'llm' ? 'tailored' : 'generated'} ·{' '}
                  {Math.max(1, Math.round((doc.size_bytes ?? 0) / 1024))} KB
                </span>
              </span>
              <button
                type="button"
                className="tiny"
                disabled={busy}
                onClick={() =>
                  act(() =>
                    api.downloadApplicationDocument(
                      application.candidate_id,
                      doc.id,
                      doc.filename,
                    ),
                  )
                }
              >
                Download
              </button>
            </div>
          ))
        ) : (
          <>
            <p className="muted">Nothing generated yet.</p>
            <div>
              <button
                type="button"
                className="tiny"
                disabled={busy}
                onClick={() => act(() => api.buildApplicationPackage(application.id))}
              >
                Build package
              </button>
            </div>
          </>
        )}
      </div>

      {application.form_answers?.length ? (
        <div className="card stack">
          <h3>Answers we would send</h3>
          {application.form_answers.map((answer) => (
            <div key={answer.key}>
              <div className="muted small">{answer.question}</div>
              <div>{answer.answer || <em className="muted">left blank on purpose</em>}</div>
              {/* Provenance, not decoration: every generated answer names the
                  preference field or resume line behind it, and "none" is how
                  a blank proves it was not invented. */}
              <div className="muted small">from: {answer.provenance || 'none'}</div>
            </div>
          ))}
        </div>
      ) : null}

      <RecordUpdate application={application} moves={moves} busy={busy} act={act} />
      <UpdateHistory application={application} />
    </div>
  );
}

/**
 * Recording what an employer did.
 *
 * The buttons alone were the whole feature, and they threw away everything
 * worth keeping: *when* it happened (a rejection heard on Friday and typed in
 * on Monday is Friday's) and *what was actually said*. Both go onto the
 * status-change event, which is what `UpdateHistory` below reads back — so
 * the trail is the same one the state machine already writes, not a second
 * record that can disagree with it.
 *
 * The status is picked first and the button confirms, rather than one button
 * per status: a note you typed cannot then be lost by clicking the wrong one.
 */
function RecordUpdate({ application, moves, busy, act }) {
  const [next, setNext] = useState('');
  const [note, setNote] = useState('');
  const [happened, setHappened] = useState('');

  if (!moves.length) {
    return (
      <div className="card stack">
        <h3>Record an update</h3>
        <p className="muted small">
          This one is closed — {statusLabel(application.status).toLowerCase()}. Nothing left to
          record.
        </p>
      </div>
    );
  }

  function submit(event) {
    event.preventDefault();
    if (!next) return;
    // Date-only input, so midday rather than midnight: a bare date parses as
    // UTC and would show as the day before for anyone west of Greenwich.
    const occurred = happened ? new Date(`${happened}T12:00:00`).toISOString() : undefined;
    act(async () => {
      await api.transitionApplication(application.id, {
        to_status: next,
        reason: note.trim(),
        occurred_at: occurred,
      });
      setNext('');
      setNote('');
      setHappened('');
    });
  }

  return (
    <form className="card stack" onSubmit={submit} data-testid="record-update">
      <h3 style={{ marginBottom: 0 }}>Record an update</h3>
      <p className="muted small" style={{ marginTop: 0 }}>
        Employer replies do not reach us on their own — the candidate&apos;s mailbox is not
        connected. Move it here when you hear something.
      </p>
      <div className="record-update-fields">
        <label className="stack" style={{ gap: 4 }}>
          <span className="muted small">What happened</span>
          <select value={next} onChange={(event) => setNext(event.target.value)}>
            <option value="">Choose an outcome…</option>
            {moves.map((move) => (
              <option key={move} value={move}>
                {statusLabel(move)}
              </option>
            ))}
          </select>
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="muted small">When (optional)</span>
          <input
            type="date"
            value={happened}
            max={new Date().toISOString().slice(0, 10)}
            onChange={(event) => setHappened(event.target.value)}
          />
        </label>
      </div>
      <label className="stack" style={{ gap: 4 }}>
        <span className="muted small">
          What was said, and who said it — this is the only record of it
        </span>
        <textarea
          rows={2}
          value={note}
          placeholder="e.g. Recruiter emailed: moving to a first-round screen next week."
          onChange={(event) => setNote(event.target.value)}
        />
      </label>
      <div>
        <button type="submit" className="primary" disabled={busy || !next}>
          Record it
        </button>
      </div>
    </form>
  );
}

/**
 * Every move this application has made, replayed from the event log.
 *
 * Two clocks on purpose: when it happened and, if they differ, when we were
 * told. Collapsing them would quietly rewrite an employer's timeline to match
 * our data entry.
 */
function UpdateHistory({ application }) {
  const entries = [...(application.history ?? [])].reverse();

  return (
    <div className="card stack">
      <h3 style={{ marginBottom: 0 }}>History</h3>
      {!entries.length ? (
        <p className="muted small" style={{ marginTop: 0 }}>
          Nothing recorded yet. Every update you record shows up here, with the note and the
          date it happened.
        </p>
      ) : (
        <div className="stack" style={{ gap: 0 }}>
          {entries.map((entry, index) => {
            const when = entry.occurred_at || entry.at;
            const loggedLater =
              entry.occurred_at && formatDate(entry.occurred_at) !== formatDate(entry.at);
            return (
              <div className="history-row" key={`${entry.at}-${index}`} data-testid="history-row">
                <div className="history-when muted small mono">{formatDate(when)}</div>
                <div className="stack" style={{ gap: 2 }}>
                  <div>
                    <ApplicationStatus status={entry.to_status} />
                    {entry.from_status ? (
                      <span className="muted small"> from {statusLabel(entry.from_status)}</span>
                    ) : null}
                    {entry.actor ? <span className="muted small"> · {entry.actor}</span> : null}
                  </div>
                  {entry.note ? <div className="small">{entry.note}</div> : null}
                  {loggedLater ? (
                    <div className="muted small">logged {formatDate(entry.at)}</div>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/**
 * Every attempt to actually send this one, newest first.
 *
 * This is the debugging surface for the submission tiers, and without it the
 * browser tier is a black box: a run that stopped because a vendor renamed a
 * field looks identical to one that never started. So it shows the verdict,
 * the vendor's own error, the fields the run filled, and the screenshots it
 * took on the way — which is also the record we would need if anyone ever
 * asked what we sent in their name.
 */
function SubmissionAttempts({ attempts }) {
  const rows = attempts.data?.submissions ?? [];
  if (attempts.loading || rows.length === 0) return null;

  return (
    <div className="card stack">
      <h3>Submission attempts</h3>
      {rows.map((row) => {
        const filled = row.run?.filled ?? {};
        const missing = row.run?.missing ?? [];
        return (
          <div key={row.id} className="stack" style={{ gap: 4 }}>
            <div className="spread">
              <span>
                <Badge tone={SUBMISSION_TONES[row.status] ?? ''}>{row.status}</Badge>
                <span className="muted small">
                  {' '}
                  · attempt {row.attempt} · {row.vendor || 'unknown ATS'} · {row.method}
                </span>
              </span>
              {row.finished_at ? (
                <span className="muted small">{formatDate(row.finished_at)}</span>
              ) : null}
            </div>
            {row.error ? <div className="error small">{row.error}</div> : null}
            {row.confirmation_ref ? (
              <div className="muted small">{row.confirmation_ref}</div>
            ) : null}
            {Object.keys(filled).length > 0 ? (
              <details>
                <summary className="muted small">
                  {Object.keys(filled).length} field
                  {Object.keys(filled).length === 1 ? '' : 's'} filled
                </summary>
                <div className="stack small" style={{ gap: 2, marginTop: 4 }}>
                  {Object.entries(filled).map(([key, value]) => (
                    <div key={key}>
                      <span className="muted">{key}:</span> {value}
                    </div>
                  ))}
                  {missing.length > 0 ? (
                    <div className="error">could not fill: {missing.join(', ')}</div>
                  ) : null}
                </div>
              </details>
            ) : null}
            {(row.artifacts ?? []).length > 0 ? (
              <div className="muted small">
                {row.artifacts.length} screenshot{row.artifacts.length === 1 ? '' : 's'} kept
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

// Reuses the tones the rest of the app speaks in rather than inventing a second
// colour vocabulary: sent is strong, waiting is partial, refused is a miss.
const SUBMISSION_TONES = {
  succeeded: 'strong',
  running: 'partial',
  queued: 'partial',
  needs_verification: 'partial',
  needs_human: 'miss',
  failed: 'miss',
};
