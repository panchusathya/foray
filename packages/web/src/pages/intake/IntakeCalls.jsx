import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../../components/common.jsx';
import { intakeApi } from '../../lib/intake.js';
import { useAsync } from '../../lib/useAsync.js';

function when(value) {
  return value ? new Date(value).toLocaleString() : '—';
}

export function IntakeCalls({ reviewNeeded, title } = {}) {
  const calls = useAsync(
    () => intakeApi.listCalls(reviewNeeded ? { review_needed: true } : {}),
    [reviewNeeded],
  );

  const rows = calls.data ?? [];

  return (
    <div className="stack" data-testid={reviewNeeded ? 'intake-review' : 'intake-calls'}>
      {title ? <h2>{title}</h2> : null}
      {calls.loading ? <Loading label="Loading calls…" /> : null}
      <ErrorBox error={calls.error} />
      {!calls.loading && !rows.length ? (
        <Empty>{reviewNeeded ? 'Nothing waiting on review.' : 'No intake calls yet.'}</Empty>
      ) : (
        <table className="jobs-table">
          <thead>
            <tr>
              <th>When</th>
              <th>From</th>
              <th>Status</th>
              <th>Gate</th>
              <th>Candidate</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} data-testid="intake-call-row">
                <td className="small muted">{when(row.started_at || row.created_at)}</td>
                <td className="mono small">{row.from_number || '—'}</td>
                <td>
                  <Badge tone={row.review_needed ? 'partial' : ''}>{row.status}</Badge>
                  {row.review_needed ? (
                    <div className="small muted">{row.review_reason}</div>
                  ) : null}
                </td>
                <td className="small">
                  {row.gate_mode}
                  {row.minutes_available != null ? ` · ${row.minutes_available} min` : ''}
                  {row.required_complete ? '' : ' · open'}
                </td>
                <td>
                  {row.candidate_id ? (
                    <Link to={`/contacts/${row.candidate_id}`}>Open record</Link>
                  ) : (
                    <span className="muted">unattributed</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
