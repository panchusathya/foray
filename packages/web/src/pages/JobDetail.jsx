import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { CandidateHit } from '../components/CandidateHit.jsx';
import { Badge, Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { jobsApi } from '../lib/jobs.js';
import { useAsync } from '../lib/useAsync.js';
import { PitchButton, ProjectLink, SourceBadge } from './JobPitch.jsx';
import './jobs.css';

function specChips(spec) {
  if (!spec) return [];
  const entries = [
    ['Titles', spec.job_titles],
    ['Seniority', spec.seniority],
    ['Must have', spec.must_have_skills],
    ['Nice to have', spec.nice_to_have_skills],
    ['Locations', spec.locations],
  ];
  return entries.filter(([, values]) => values?.length);
}

export function JobDetailPage() {
  const { postingId } = useParams();
  const posting = useAsync(() => jobsApi.get(postingId), [postingId]);
  const matches = useAsync(() => jobsApi.matches(postingId), [postingId]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function rematch() {
    setBusy(true);
    setError(null);
    try {
      await jobsApi.rematch(postingId);
      posting.reload();
      matches.reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  // Through the page's ErrorBox, not bare: a 409 or a dropped connection on a
  // shortlist otherwise leaves the row unchanged with nothing said, and the
  // recruiter believes a candidate is shortlisted who was never written.
  async function setMatchStatus(matchId, status) {
    setError(null);
    try {
      await jobsApi.patchMatch(matchId, { status });
      matches.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  if (posting.loading) return <Loading />;
  if (!posting.data) {
    return (
      <div className="stack">
        <PageHeader title="Job posting" />
        <Empty>That posting was not found.</Empty>
      </div>
    );
  }

  const row = posting.data;
  const chips = specChips(row.parsed_spec);

  return (
    <div className="stack">
      <PageHeader
        title={row.title}
        subtitle={[row.company, row.location].filter(Boolean).join(' · ') || 'Job posting'}
      />
      <ErrorBox error={error} />
      <div className="jobs-detail-cta card">
        <div>
          <strong>{row.project_id ? 'This posting is linked to a project.' : 'Pitch this role.'}</strong>
          <p className="small muted" style={{ margin: '4px 0 0' }}>
            {row.project_id
              ? 'Open Sequences to start outreach. Shortlist here does not enrol anyone.'
              : 'Create a project from this posting, then Sequences to send. Nothing auto-enrols.'}
          </p>
        </div>
        <div className="row wrap">
          <PitchButton posting={row} onError={setError} className="primary" />
          <ProjectLink posting={row} />
        </div>
      </div>
      <div className="row wrap">
        <SourceBadge source={row.source} />
        <Badge>{row.status}</Badge>
        {row.has_warm_path ? <Badge tone="accent">warm path</Badge> : null}
        {row.fee_bearing ? <Badge tone="strong">{row.fee_terms || 'fee'}</Badge> : null}
        <button type="button" className="tiny ghost" onClick={rematch} disabled={busy}>
          Recompute matches
        </button>
      </div>

      {row.parsed_at ? (
        <div className="card">
          <strong className="small">Parsed spec</strong>
          <div className="row wrap" style={{ marginTop: 8 }}>
            {chips.map(([label, values]) => (
              <Badge key={label}>
                {label}: {values.join(', ')}
              </Badge>
            ))}
            {!chips.length ? <span className="muted small">no structured filters yet</span> : null}
          </div>
        </div>
      ) : (
        <div className="card muted small" data-testid="pending-parse">
          Waiting for parse…
        </div>
      )}

      <div className="card">
        <strong className="small">Job description</strong>
        <div className="jd-body">{row.jd_text || '—'}</div>
      </div>

      {row.referral_paths?.length ? (
        <div className="card stack">
          <strong className="small">Referral paths</strong>
          {row.referral_paths.map((path) => (
            <div key={path.candidate_id} className="row spread small">
              <Link to={`/contacts/${path.candidate_id}`}>{path.name}</Link>
              <span className="muted">
                {path.company} · {path.basis}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      <div className="stack">
        <strong className="small">Ranked contacts</strong>
        {matches.loading ? <Loading /> : null}
        {(matches.data ?? []).map((match, index) =>
          match.candidate ? (
          <div key={match.id} data-testid="job-match">
            <CandidateHit
              hit={{
                candidate: { ...match.candidate, skills: match.candidate.skills ?? [] },
                criteria: match.criteria ?? [],
                final_score: match.score,
                rank: index + 1,
              }}
            />
            <div className="row wrap" style={{ marginTop: -8, marginBottom: 12 }}>
              <button
                type="button"
                className="tiny"
                onClick={() => setMatchStatus(match.id, 'shortlisted')}
              >
                Shortlist
              </button>
              <button
                type="button"
                className="tiny ghost"
                onClick={() => setMatchStatus(match.id, 'dismissed')}
              >
                Dismiss
              </button>
            </div>
          </div>
          ) : null,
        )}
        {!matches.loading && !(matches.data ?? []).length ? (
          <Empty>No contacts above the score floor yet.</Empty>
        ) : null}
      </div>
    </div>
  );
}
