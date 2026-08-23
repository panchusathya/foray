import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../../components/common.jsx';
import { api } from '../../lib/api.js';
import { formatComp, formatTimeline } from '../../lib/intake.js';
import { jobsApi } from '../../lib/jobs.js';
import { useAsync } from '../../lib/useAsync.js';

function MatchesFor({ candidate }) {
  const matches = useAsync(() => jobsApi.forCandidate(candidate.id), [candidate.id]);
  const rows = matches.data ?? [];
  return (
    <div className="stack" style={{ gap: 6 }}>
      {(rows.slice(0, 5) || []).map((match) => (
        <div key={match.id} className="row spread">
          <Link to={`/jobs/${match.job_posting_id}`}>
            {match.posting?.title || match.job_posting_id}
          </Link>
          <Badge>{Math.round((match.score ?? 0) * 100)}</Badge>
        </div>
      ))}
      {!matches.loading && !rows.length ? <span className="muted small">No matches yet.</span> : null}
    </div>
  );
}

export function IntakeMatches() {
  const people = useAsync(
    () => api.contactsGrid({ scope: 'voice_intake', limit: 50, sort: 'created', dir: 'desc' }),
    [],
  );
  const rows = people.data ?? [];

  return (
    <div className="stack" data-testid="intake-matches">
      {people.loading ? <Loading label="Loading matches…" /> : null}
      <ErrorBox error={people.error} />
      {!people.loading && !rows.length ? (
        <Empty>No intake candidates to match yet.</Empty>
      ) : (
        rows.map((person) => (
          <div key={person.id} className="card stack" data-testid="intake-match-row">
            <div className="row spread">
              <div>
                <Link to={`/contacts/${person.id}`} style={{ fontWeight: 600 }}>
                  {person.full_name}
                </Link>
                <div className="small muted">
                  {formatTimeline(person.intake_timeline)} · {formatComp(person.intake_target_comp)}
                  {person.intake_confirmed ? ' · confirmed' : ''}
                </div>
              </div>
              <Badge>{person.intake_match_count ?? 0} matches</Badge>
            </div>
            <MatchesFor candidate={person} />
          </div>
        ))
      )}
    </div>
  );
}
