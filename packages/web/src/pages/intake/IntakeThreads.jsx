import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../../components/common.jsx';
import { intakeApi } from '../../lib/intake.js';
import { useAsync } from '../../lib/useAsync.js';

function when(value) {
  return value ? new Date(value).toLocaleString() : '—';
}

export function IntakeThreads() {
  const threads = useAsync(() => intakeApi.listThreads({}), []);
  const rows = threads.data ?? [];

  return (
    <div className="stack" data-testid="intake-threads">
      {threads.loading ? <Loading label="Loading threads…" /> : null}
      <ErrorBox error={threads.error} />
      {!threads.loading && !rows.length ? (
        <Empty>No messaging intake threads yet.</Empty>
      ) : (
        <table className="jobs-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Channel</th>
              <th>Contact</th>
              <th>Status</th>
              <th>Messages</th>
              <th>Candidate</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const who = row.contact_name || row.contact_email || row.from_address || '—';
              return (
                <tr key={row.id} data-testid="intake-thread-row">
                  <td className="small muted">{when(row.last_inbound_at || row.created_at)}</td>
                  <td>
                    <Badge>{row.channel}</Badge>
                  </td>
                  <td className="small">{who}</td>
                  <td>
                    <Badge tone={row.review_needed ? 'partial' : ''}>{row.status}</Badge>
                    {row.review_needed ? (
                      <div className="small muted">{row.review_reason}</div>
                    ) : null}
                    {row.required_complete ? null : (
                      <div className="small muted">gate open</div>
                    )}
                  </td>
                  <td className="small muted">
                    {row.inbound_count} in / {row.outbound_count} out
                  </td>
                  <td>
                    {row.candidate_id ? (
                      <Link to={`/contacts/${row.candidate_id}`}>Open record</Link>
                    ) : (
                      <span className="muted">unattributed</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
