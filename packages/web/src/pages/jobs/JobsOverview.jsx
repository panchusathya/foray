import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../../components/common.jsx';
import { jobsApi } from '../../lib/jobs.js';
import { useAsync } from '../../lib/useAsync.js';
import { PitchButton, SourceBadge } from '../JobPitch.jsx';

export function JobsOverview() {
  const [filters, setFilters] = useState({
    q: '',
    status: '',
    source: '',
    fee_bearing: '',
    has_warm_path: '',
  });
  const [error, setError] = useState(null);
  const list = useAsync(
    () =>
      jobsApi.list({
        q: filters.q,
        status: filters.status || undefined,
        source: filters.source || undefined,
        fee_bearing: filters.fee_bearing === '' ? undefined : filters.fee_bearing === 'true',
        has_warm_path: filters.has_warm_path === '' ? undefined : filters.has_warm_path === 'true',
      }),
    [filters.q, filters.status, filters.source, filters.fee_bearing, filters.has_warm_path],
  );

  const items = list.data?.items ?? [];

  return (
    <div className="stack">
      <ErrorBox error={error} />
      <div className="jobs-filters">
        <input
          type="search"
          placeholder="Search title or company"
          value={filters.q}
          onChange={(event) => setFilters({ ...filters, q: event.target.value })}
        />
        <select
          value={filters.source}
          onChange={(event) => setFilters({ ...filters, source: event.target.value })}
          data-testid="jobs-source-filter"
        >
          <option value="">Any source</option>
          <option value="exa">Exa (market)</option>
          <option value="project">Our mandates</option>
          <option value="manual">Manual</option>
          <option value="csv">CSV</option>
        </select>
        <select
          value={filters.status}
          onChange={(event) => setFilters({ ...filters, status: event.target.value })}
        >
          <option value="">Any status</option>
          <option value="pending_parse">Pending parse</option>
          <option value="open">Open</option>
          <option value="stale">Stale</option>
          <option value="closed">Closed</option>
          <option value="archived">Archived</option>
        </select>
        <select
          value={filters.fee_bearing}
          onChange={(event) => setFilters({ ...filters, fee_bearing: event.target.value })}
        >
          <option value="">Fee: any</option>
          <option value="true">Fee-bearing</option>
          <option value="false">No fee</option>
        </select>
        <select
          value={filters.has_warm_path}
          onChange={(event) => setFilters({ ...filters, has_warm_path: event.target.value })}
        >
          <option value="">Warm path: any</option>
          <option value="true">Has warm path</option>
          <option value="false">No warm path</option>
        </select>
      </div>

      {list.loading ? <Loading /> : null}
      {!list.loading && !items.length ? (
        <Empty>No job postings yet.</Empty>
      ) : (
        <div className="card" style={{ overflowX: 'auto' }}>
          <table className="jobs-table" data-testid="jobs-table">
            <thead>
              <tr>
                <th>Company</th>
                <th>Title</th>
                <th>Location</th>
                <th>Source</th>
                <th>Status</th>
                <th>Matches</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((posting) => (
                <tr key={posting.id} data-testid="job-row">
                  <td>
                    <Link to={`/jobs/${posting.id}`}>{posting.company || '—'}</Link>
                  </td>
                  <td>
                    <Link to={`/jobs/${posting.id}`}>{posting.title}</Link>
                    {/* The scraped JD is the thing people actually want to
                        read before pitching, and it was three clicks away
                        behind the detail page. Mandates we typed in ourselves
                        have no URL, so the link is conditional rather than a
                        dead "JD" on every row. */}
                    {posting.source_url ? (
                      <>
                        {' '}
                        <a
                          className="jd-link"
                          href={posting.source_url}
                          target="_blank"
                          rel="noreferrer"
                          title={posting.source_url}
                          onClick={(event) => event.stopPropagation()}
                        >
                          JD ↗
                        </a>
                      </>
                    ) : null}
                  </td>
                  <td>{posting.location || '—'}</td>
                  <td>
                    <SourceBadge source={posting.source} />
                  </td>
                  <td>
                    <Badge>{posting.status}</Badge>
                  </td>
                  <td>{posting.match_count}</td>
                  <td className="jobs-row-actions">
                    <PitchButton posting={posting} onError={setError} />
                    {posting.has_warm_path ? <Badge tone="accent">warm path</Badge> : null}
                    {posting.fee_bearing ? <Badge tone="strong">fee</Badge> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
