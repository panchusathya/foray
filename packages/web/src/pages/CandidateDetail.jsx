/**
 * The candidate detail page: profile on the left, contact history on the
 * right. First real consumer of api.getCandidate — every people surface
 * (Contacts, search hits, pipeline cards, Sourcing) links here.
 */

import { Link, useParams } from 'react-router-dom';

import { ContactHistory } from '../components/ContactHistory.jsx';
import { Badge, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';

export function CandidateDetailPage() {
  const { candidateId } = useParams();
  const candidate = useAsync(() => api.getCandidate(candidateId), [candidateId]);

  if (candidate.loading) return <Loading label="Loading candidate…" />;
  if (candidate.error) return <ErrorBox error={candidate.error} />;
  const person = candidate.data;

  const location = [person.location_city, person.location_region, person.location_country]
    .filter(Boolean)
    .join(', ');

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

      <div className="grid two" style={{ alignItems: 'start' }}>
        <div className="stack">
          <div className="card stack" data-testid="candidate-profile">
            <strong className="small">Profile</strong>
            {location ? <div className="small muted">{location}</div> : null}
            {person.years_experience ? (
              <div className="small muted">{person.years_experience} years of experience</div>
            ) : null}
            <div className="row wrap">
              {(person.skills ?? []).slice(0, 12).map((skill) => (
                <Badge key={skill}>{skill}</Badge>
              ))}
            </div>
            <div className="row wrap small">
              {(person.emails ?? []).map((email) => (
                <span key={email} className="mono muted">
                  {email}
                </span>
              ))}
            </div>
            <div className="row wrap small">
              {person.linkedin_url ? (
                <a href={person.linkedin_url} target="_blank" rel="noreferrer">
                  LinkedIn
                </a>
              ) : null}
              {person.github_url ? (
                <a href={person.github_url} target="_blank" rel="noreferrer">
                  GitHub
                </a>
              ) : null}
              {person.website_url ? (
                <a href={person.website_url} target="_blank" rel="noreferrer">
                  Website
                </a>
              ) : null}
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
                    {[experience.start_date, experience.end_date || (experience.is_current ? 'now' : '')]
                      .filter(Boolean)
                      .join(' → ')}
                  </div>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        <div className="stack">
          <strong className="small">Contact history</strong>
          <ContactHistory candidateId={candidateId} />
        </div>
      </div>
    </div>
  );
}
