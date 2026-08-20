import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../components/common.jsx';
import { jobsApi } from '../lib/jobs.js';
import { useAsync } from '../lib/useAsync.js';
import { PitchButton, SourceBadge } from './JobPitch.jsx';
import './jobs.css';

function formatDay(value) {
  if (!value) return '';
  return new Date(value).toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
  });
}

export function RematchPanel() {
  const queue = useAsync(() => jobsApi.rematchQueue(), []);
  const [error, setError] = useState(null);

  async function setStatus(matchId, status) {
    setError(null);
    try {
      await jobsApi.patchMatch(matchId, { status });
      queue.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  const rows = queue.data ?? [];

  return (
    <div className="stack">
      <ErrorBox error={error} />
      {queue.loading ? <Loading /> : null}
      {!queue.loading && !rows.length ? (
        <Empty>Nobody in the rematch queue.</Empty>
      ) : (
        rows.map((row) => (
          <div key={row.candidate.id} className="card rematch-row" data-testid="rematch-row">
            <div>
              <Link to={`/contacts/${row.candidate.id}`} style={{ fontWeight: 600 }}>
                {row.candidate.full_name}
              </Link>
              <div className="small muted">
                declined {row.declined_label || 'a role'}
                {row.declined_on ? ` on ${formatDay(row.declined_on)}` : ''}
                {' — '}
                {row.matches.length} better fit{row.matches.length === 1 ? '' : 's'}
              </div>
            </div>
            <div className="rematch-fits">
              {row.matches.map((match) => {
                const posting = match.posting;
                return (
                  <div key={match.id} className="row spread" data-testid="rematch-fit">
                    <div>
                      <Link to={`/jobs/${match.job_posting_id}`}>
                        {posting?.title || match.job_posting_id}
                      </Link>
                      <div className="small muted">
                        {posting?.company || ''}
                        {posting?.has_warm_path ? ' · warm path' : ''}
                        {posting?.fee_bearing ? ' · fee' : ''}
                      </div>
                      {posting?.source ? (
                        <div style={{ marginTop: 4 }}>
                          <SourceBadge source={posting.source} />
                        </div>
                      ) : null}
                    </div>
                    <div className="row wrap">
                      <Badge>{Math.round(match.score * 100)}</Badge>
                      <button
                        type="button"
                        className="tiny"
                        onClick={() => setStatus(match.id, 'shortlisted')}
                      >
                        Shortlist
                      </button>
                      <button
                        type="button"
                        className="tiny ghost"
                        onClick={() => setStatus(match.id, 'dismissed')}
                      >
                        Dismiss
                      </button>
                      <PitchButton
                        posting={posting || { id: match.job_posting_id, project_id: null }}
                        onError={setError}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))
      )}
    </div>
  );
}
