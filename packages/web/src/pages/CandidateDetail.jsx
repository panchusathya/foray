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
import { Link, useParams } from 'react-router-dom';

import { ContactHistory } from '../components/ContactHistory.jsx';
import { InlineField } from '../components/InlineField.jsx';
import { NotesPanel } from '../components/NotesPanel.jsx';
import { Badge, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { CRM_STATUSES, CRM_STATUS_LABELS, api } from '../lib/api.js';
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

  const status = person.crm_status || 'none';

  return (
    <div className="stack">
      <PageHeader
        title={person.full_name}
        subtitle={
          [person.current_title, person.current_company].filter(Boolean).join(' · ') ||
          person.headline ||
          ''
        }
        actions={
          <Link className="badge" to="/contacts">
            ← Contacts
          </Link>
        }
      />

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
            {person.years_experience ? (
              <div className="small muted">{person.years_experience} years of experience</div>
            ) : null}
            <div className="row wrap">
              {(person.skills ?? []).slice(0, 12).map((skill) => (
                <Badge key={skill}>{skill}</Badge>
              ))}
            </div>
            <div className="row wrap small">
              {/* Read-only: the address is the key every mail-matching path
                  reads, and re-keying it needs its own deliberate flow. */}
              {(person.emails ?? []).map((email) => (
                <span key={email} className="mono muted">
                  {email}
                </span>
              ))}
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
              Emails
            </button>
            <button
              type="button"
              className={tab === 'notes' ? 'active' : ''}
              data-testid="tab-notes"
              onClick={() => setTab('notes')}
            >
              Notes
            </button>
          </div>

          {/* Both stay mounted: the Emails tab must be present on first render
              (see the file header), and hiding rather than unmounting keeps a
              half-typed note alive when you glance at the thread. */}
          <div hidden={tab !== 'emails'}>
            <ContactHistory candidateId={candidateId} />
          </div>
          <div hidden={tab !== 'notes'}>
            <NotesPanel candidateId={candidateId} />
          </div>
        </div>
      </div>
    </div>
  );
}
