import { useState } from 'react';
import { Link } from 'react-router-dom';

import { AvailabilityBadge, AvailabilityDetail } from './Availability.jsx';
import { Badge, ScoreBar } from './common.jsx';

const STATUS_ORDER = { miss: 0, partial: 1, unknown: 2, strong: 3 };

/**
 * One search result. The evidence list is the point of the product: a recruiter
 * has to be able to see *why* this person surfaced without opening the profile.
 */
export function CandidateHit({ hit, selected, onToggle }) {
  const [expanded, setExpanded] = useState(false);
  const { candidate, criteria = [], availability } = hit;

  // Show what's wrong first — that's what a recruiter scans for.
  const ordered = [...criteria].sort(
    (a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9),
  );
  const visible = expanded ? ordered : ordered.slice(0, 4);
  const location = [candidate.location_city, candidate.location_country].filter(Boolean).join(', ');

  return (
    <div className="card hit">
      <div className="hit-rank">
        {onToggle ? (
          <input
            type="checkbox"
            checked={Boolean(selected)}
            onChange={() => onToggle(candidate.id)}
            aria-label={`Select ${candidate.full_name}`}
          />
        ) : (
          hit.rank
        )}
      </div>

      <div className="hit-body">
        <div className="spread">
          <div>
            <h3>
              <Link to={`/contacts/${candidate.id}`} style={{ textDecoration: 'none' }}>
                {candidate.full_name}
              </Link>
            </h3>
            <div className="meta">
              {candidate.current_title || '—'}
              {candidate.current_company ? ` · ${candidate.current_company}` : ''}
              {location ? ` · ${location}` : ''}
            </div>
            <div className="row wrap" style={{ marginTop: 6 }}>
              <AvailabilityBadge availability={availability} />
              {candidate.crm_status && candidate.crm_status !== 'none' ? (
                <Badge tone={candidate.crm_status === 'do_not_contact' ? 'miss' : ''}>
                  {candidate.crm_status.replace(/_/g, ' ')}
                </Badge>
              ) : null}
            </div>
          </div>
          <div className="score">
            <div className="score-value">{(hit.final_score * 100).toFixed(0)}</div>
            <div className="small muted">match</div>
          </div>
        </div>

        <ScoreBar value={hit.final_score} />

        <AvailabilityDetail availability={availability} />

        <div className="row wrap" style={{ marginTop: 10 }}>
          <Badge>{candidate.seniority}</Badge>
          <Badge>{candidate.years_experience}y exp</Badge>
          {candidate.skills.slice(0, 5).map((skill) => (
            <Badge key={skill}>{skill}</Badge>
          ))}
          {hit.rerank_score != null ? <Badge tone="accent">AI reviewed</Badge> : null}
        </div>

        {hit.summary ? (
          <p className="small" style={{ marginBottom: 0 }}>
            {hit.summary}
          </p>
        ) : null}

        {hit.concerns?.length ? (
          <p className="small" style={{ color: 'var(--partial)' }}>
            Watch out: {hit.concerns.join('; ')}
          </p>
        ) : null}

        {visible.length ? (
          <div className="criteria">
            {visible.map((criterion) => (
              <div className="criterion" key={criterion.name}>
                <span className={`verdict ${criterion.status}`}>
                  {{ strong: 'Good match', partial: 'Partial', miss: 'No match' }[
                    criterion.status
                  ] ?? 'Unknown'}
                </span>
                <span className="label">{criterion.label}</span>
                <span className="evidence">— {criterion.evidence}</span>
              </div>
            ))}
          </div>
        ) : null}

        <div className="row" style={{ marginTop: 10 }}>
          {ordered.length > 4 ? (
            <button type="button" className="tiny ghost" onClick={() => setExpanded(!expanded)}>
              {expanded ? 'Show less' : `Show all ${ordered.length} criteria`}
            </button>
          ) : null}
          {candidate.linkedin_url ? (
            <a
              className="badge"
              href={candidate.linkedin_url}
              target="_blank"
              rel="noreferrer noopener"
            >
              LinkedIn
            </a>
          ) : null}
          {candidate.emails?.[0] ? <span className="badge mono">{candidate.emails[0]}</span> : null}
        </div>
      </div>
    </div>
  );
}
