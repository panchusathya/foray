/**
 * Contacts — the global candidate browser. Debounced text search over
 * GET /v1/candidates with limit/offset paging.
 */

import { useEffect, useState } from 'react';

import { Badge, Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';

const PAGE_SIZE = 50;

function location(candidate) {
  return [candidate.location_city, candidate.location_country].filter(Boolean).join(', ');
}

export function ContactsPage() {
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [page, setPage] = useState(0);

  useEffect(() => {
    const handle = setTimeout(() => {
      setDebounced(query.trim());
      setPage(0);
    }, 300);
    return () => clearTimeout(handle);
  }, [query]);

  const candidates = useAsync(
    () => api.listCandidates({ q: debounced, limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
    [debounced, page],
  );
  const count = useAsync(() => api.candidateCount(), []);

  const rows = candidates.data ?? [];
  const total = count.data?.count;

  return (
    <>
      <PageHeader
        title="Contacts"
        subtitle={total != null ? `${total} candidates in your workspace.` : 'All candidates in your workspace.'}
      />

      <div className="stack">
        <input
          type="text"
          placeholder="Search by name, title, or company…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />

        <ErrorBox error={candidates.error} />
        {candidates.loading ? (
          <Loading label="Loading candidates…" />
        ) : rows.length ? (
          <div className="stack" style={{ gap: 8 }}>
            {rows.map((candidate) => (
              <div key={candidate.id} className="card spread">
                <div>
                  <div style={{ fontWeight: 600 }}>{candidate.full_name}</div>
                  <div className="muted small">
                    {[candidate.current_title, candidate.current_company]
                      .filter(Boolean)
                      .join(' · ')}
                    {location(candidate) ? ` · ${location(candidate)}` : ''}
                  </div>
                </div>
                <div className="row wrap" style={{ justifyContent: 'flex-end' }}>
                  {(candidate.skills ?? []).slice(0, 4).map((skill) => (
                    <Badge key={skill}>{skill}</Badge>
                  ))}
                  {candidate.years_experience ? (
                    <Badge tone="accent">{Math.round(candidate.years_experience)}y</Badge>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <Empty>
            {debounced ? `No candidates match “${debounced}”.` : 'No candidates yet — import some via the ingest CLI.'}
          </Empty>
        )}

        <div className="row" style={{ justifyContent: 'space-between' }}>
          <button type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>
            Previous
          </button>
          <span className="muted small">Page {page + 1}</span>
          <button
            type="button"
            disabled={rows.length < PAGE_SIZE}
            onClick={() => setPage(page + 1)}
          >
            Next
          </button>
        </div>
      </div>
    </>
  );
}
