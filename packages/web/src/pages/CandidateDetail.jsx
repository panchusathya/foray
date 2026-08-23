/**
 * The person record: profile and quick actions on the left, their history on
 * the right.
 *
 * The Emails tab is the default and is always mounted. Lazy-mounting it would
 * be the obvious optimisation and would break the contract that landing on
 * /contacts/:id shows you the correspondence — which is the whole reason
 * anyone opens this page.
 */

import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { ContactHistory } from '../components/ContactHistory.jsx';
import { EmailEditor } from '../components/EmailEditor.jsx';
import { InlineField } from '../components/InlineField.jsx';
import { NotesPanel } from '../components/NotesPanel.jsx';
import { TagEditor } from '../components/TagEditor.jsx';
import { Avatar, Badge, ErrorBox, Loading } from '../components/common.jsx';
import { CRM_STATUSES, CRM_STATUS_LABELS, api } from '../lib/api.js';
import { formatComp, formatTimeline, intakeApi } from '../lib/intake.js';
import { jobsApi } from '../lib/jobs.js';
import { useAsync } from '../lib/useAsync.js';

const EDITABLE = [
  ['current_title', 'Title'],
  ['current_company', 'Company'],
  ['location_city', 'City'],
  ['location_region', 'Region'],
  ['location_country', 'Country'],
  ['linkedin_url', 'LinkedIn'],
  ['github_url', 'GitHub'],
  ['website_url', 'Website'],
];

export function CandidateDetailPage() {
  const { candidateId } = useParams();
  const candidate = useAsync(() => api.getCandidate(candidateId), [candidateId]);
  const [tab, setTab] = useState('emails');
  const [saveError, setSaveError] = useState(null);
  const [deleting, setDeleting] = useState(false);
  // null until the panel has actually loaded: a tab reading "Notes 0" before
  // the request lands says something false for as long as it takes.
  const [emailCount, setEmailCount] = useState(null);
  const [noteCount, setNoteCount] = useState(null);
  const navigate = useNavigate();

  if (candidate.loading) return <Loading label="Loading candidate…" />;
  if (candidate.error) return <ErrorBox error={candidate.error} />;
  const person = candidate.data;

  async function save(field, value) {
    setSaveError(null);
    try {
      await api.updateCandidate(candidateId, { [field]: value });
      candidate.reload();
    } catch (caught) {
      setSaveError(caught);
      throw caught;
    }
  }

  async function remove() {
    // Spelling out what goes with them: the API cascades to notes, tags, the
    // address projection, project memberships and enrollments, and there is no
    // undo. A count is more use here than the word "permanently".
    const owned = [
      (person.emails ?? []).length && `${person.emails.length} address(es)`,
      (person.tags ?? []).length && `${person.tags.length} tag(s)`,
    ].filter(Boolean);
    if (
      !window.confirm(
        `Delete ${person.full_name}?\n\n` +
          `This removes the person and everything attached to them` +
          `${owned.length ? ` — including ${owned.join(' and ')}` : ''}` +
          `, plus any notes, project memberships and sequence enrolments.\n\n` +
          `There is no undo. To fix a bad address instead, edit the ` +
          `Email addresses list.`,
      )
    ) {
      return;
    }
    setDeleting(true);
    setSaveError(null);
    try {
      await api.deleteCandidate(candidateId);
      navigate('/contacts');
    } catch (caught) {
      setSaveError(caught);
      setDeleting(false);
    }
  }

  const status = person.crm_status || 'none';

  return (
    <div className="stack">
      {/* A record header rather than the generic PageHeader: who this is, how
          to reach them, and where they stand, all above the fold. */}
      <div className="record-head">
        <Avatar name={person.full_name} size={44} />
        <div className="record-id">
          <h1>{person.full_name}</h1>
          <p>
            {[person.current_title, person.current_company].filter(Boolean).join(' · ') ||
              person.headline ||
              'No title on file'}
          </p>
          <p className="mono small muted">{(person.emails ?? [])[0] || 'No email on file'}</p>
        </div>
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          {status !== 'none' ? (
            <span className={`badge ${status === 'do_not_contact' ? 'miss' : 'accent'}`}>
              {CRM_STATUS_LABELS[status] ?? status}
            </span>
          ) : null}
          <button
            type="button"
            className="tiny ghost"
            data-testid="delete-candidate"
            disabled={deleting}
            onClick={remove}
          >
            {deleting ? 'Deleting…' : 'Delete contact'}
          </button>
          <Link className="badge" to="/contacts">
            ← Contacts
          </Link>
        </div>
      </div>

      <ErrorBox error={saveError} />

      <div className="grid two" style={{ alignItems: 'start' }}>
        <div className="stack">
          <div className="card row wrap" data-testid="candidate-actions" style={{ gap: 10 }}>
            <label className="row small" style={{ gap: 6 }}>
              Status
              <select
                value={status}
                data-testid="crm-status"
                onChange={(event) => save('crm_status', event.target.value)}
              >
                {CRM_STATUSES.map((value) => (
                  <option key={value} value={value}>
                    {CRM_STATUS_LABELS[value]}
                  </option>
                ))}
              </select>
            </label>
            {status === 'do_not_contact' ? (
              /* Badge does not forward props, and this needs a testid. */
              <span className="badge miss" data-testid="dnc-badge">
                suppressed — sequences will skip them
              </span>
            ) : null}
          </div>

          <div className="card stack" data-testid="candidate-profile">
            <strong className="small">Profile</strong>
            {EDITABLE.map(([field, label]) => (
              <InlineField
                key={field}
                label={label}
                value={person[field] ?? ''}
                testId={`field-${field}`}
                onSave={(value) => save(field, value)}
              />
            ))}
            <TagEditor
              candidateId={candidateId}
              tags={person.tags}
              onChanged={candidate.reload}
            />
            {person.years_experience ? (
              <div className="small muted">{person.years_experience} years of experience</div>
            ) : null}
            <div className="row wrap">
              {(person.skills ?? []).slice(0, 12).map((skill) => (
                <Badge key={skill}>{skill}</Badge>
              ))}
            </div>
            <div className="stack small">
              <strong className="small">Email addresses</strong>
              <EmailEditor
                candidateId={candidateId}
                emails={person.emails}
                onChanged={candidate.reload}
              />
            </div>
          </div>

          {(person.experiences ?? []).length ? (
            <div className="card stack">
              <strong className="small">Experience</strong>
              {person.experiences.map((experience) => (
                <div key={experience.id} className="small">
                  <div>
                    {experience.title || '—'}
                    {experience.company ? ` · ${experience.company}` : ''}
                  </div>
                  <div className="muted">
                    {[
                      experience.start_date,
                      experience.end_date || (experience.is_current ? 'now' : ''),
                    ]
                      .filter(Boolean)
                      .join(' → ')}
                  </div>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        <div className="stack">
          <div className="record-tabs" data-testid="record-tabs">
            <button
              type="button"
              className={tab === 'emails' ? 'active' : ''}
              data-testid="tab-emails"
              onClick={() => setTab('emails')}
            >
              Emails{' '}
              {emailCount === null ? null : <span className="tab-count">{emailCount}</span>}
            </button>
            <button
              type="button"
              className={tab === 'notes' ? 'active' : ''}
              data-testid="tab-notes"
              onClick={() => setTab('notes')}
            >
              Notes{' '}
              {noteCount === null ? null : <span className="tab-count">{noteCount}</span>}
            </button>
            <button
              type="button"
              className={tab === 'intake' ? 'active' : ''}
              data-testid="tab-intake"
              onClick={() => setTab('intake')}
            >
              Intake
            </button>
            <button
              type="button"
              className={tab === 'jobs' ? 'active' : ''}
              data-testid="tab-jobs"
              onClick={() => setTab('jobs')}
            >
              Jobs
            </button>
          </div>

          {/* Emails and notes stay mounted: the Emails tab must be present on
              first render (see the file header), and hiding rather than
              unmounting keeps a half-typed note alive when you glance at the
              thread. */}
          <div hidden={tab !== 'emails'}>
            <ContactHistory candidateId={candidateId} onCount={setEmailCount} />
          </div>
          <div hidden={tab !== 'notes'}>
            <NotesPanel candidateId={candidateId} onCount={setNoteCount} />
          </div>
          <div hidden={tab !== 'intake'}>
            <IntakePanel candidateId={candidateId} />
          </div>
          <div hidden={tab !== 'jobs'}>
            <JobsPanel candidateId={candidateId} />
          </div>
        </div>
      </div>
    </div>
  );
}

function fieldValue(answer) {
  if (answer.declined) return 'declined';
  if (answer.value == null || answer.value === '' || answer.value === false) {
    if (answer.asked === false) return 'deferred';
    return '—';
  }
  if (Array.isArray(answer.value)) return answer.value.join(', ') || '—';
  if (typeof answer.value === 'object') return JSON.stringify(answer.value);
  return String(answer.value);
}

function IntakePanel({ candidateId }) {
  const pref = useAsync(() => intakeApi.getPreference(candidateId), [candidateId]);
  const call = useAsync(async () => {
    if (!pref.data?.source_call_id) return null;
    return intakeApi.getCall(pref.data.source_call_id);
  }, [pref.data?.source_call_id]);
  const [confirmError, setConfirmError] = useState(null);

  if (pref.loading) return <Loading label="Loading intake…" />;
  if (pref.error && pref.error.status === 404) {
    return <div className="card empty">No intake record for this person yet.</div>;
  }
  if (pref.error) return <ErrorBox error={pref.error} />;

  const record = pref.data;
  const answers = call.data?.field_answers ?? [];

  async function confirm() {
    setConfirmError(null);
    try {
      await intakeApi.confirmPreference(candidateId);
      pref.reload();
    } catch (caught) {
      setConfirmError(caught);
    }
  }

  return (
    <div className="stack" data-testid="intake-panel">
      <ErrorBox error={confirmError} />
      <div className="card stack" style={{ gap: 8 }}>
        <div className="row spread">
          <div>
            <div className="small muted">Timeline</div>
            <div>{formatTimeline(record.timeline)}</div>
          </div>
          <div>
            <div className="small muted">Target comp</div>
            <div>{formatComp(record.target_total || record.target_base)}</div>
          </div>
          <div>
            <div className="small muted">Work auth</div>
            <div>{record.work_authorization || '—'}</div>
          </div>
        </div>
        {record.review_needed ? (
          <div className="small muted">Review: {record.review_reason || 'needs a look'}</div>
        ) : null}
        {record.confirmed_at ? (
          <Badge>confirmed</Badge>
        ) : (
          <button type="button" className="tiny" onClick={confirm}>
            Confirm record
          </button>
        )}
      </div>
      {answers.length ? (
        <table className="jobs-table">
          <thead>
            <tr>
              <th>Field</th>
              <th>Value</th>
              <th>Said</th>
            </tr>
          </thead>
          <tbody>
            {answers.map((answer) => (
              <tr key={answer.id || answer.field_key}>
                <td className="small">{answer.field_key}</td>
                <td>{fieldValue(answer)}</td>
                <td className="small muted">{answer.verbatim || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="card empty">No per-field provenance on the source call.</div>
      )}
    </div>
  );
}

function JobsPanel({ candidateId }) {
  const matches = useAsync(() => jobsApi.forCandidate(candidateId), [candidateId]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const rows = matches.data ?? [];

  async function rematch() {
    setBusy(true);
    setError(null);
    try {
      await jobsApi.rematchCandidate(candidateId);
      matches.reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  if (matches.loading) return <Loading label="Loading jobs…" />;
  if (matches.error) return <ErrorBox error={matches.error} />;

  return (
    <div className="stack" data-testid="jobs-panel">
      <ErrorBox error={error} />
      <div className="row">
        <button type="button" className="tiny" onClick={rematch} disabled={busy}>
          Rematch
        </button>
      </div>
      {!rows.length ? (
        <div className="card empty">No job matches yet.</div>
      ) : (
        rows.map((match) => (
          <div key={match.id} className="card row spread">
            <div>
              <Link to={`/jobs/${match.job_posting_id}`}>
                {match.posting?.title || match.job_posting_id}
              </Link>
              <div className="small muted">{match.posting?.company}</div>
            </div>
            <Badge>{Math.round((match.score ?? 0) * 100)}</Badge>
          </div>
        ))
      )}
    </div>
  );
}
