import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { jobsApi, parseCsv } from '../lib/jobs.js';
import { useAsync } from '../lib/useAsync.js';
import './jobs.css';

export function JobsPage() {
  const [filters, setFilters] = useState({ q: '', status: '', fee_bearing: '', has_warm_path: '' });
  const [form, setForm] = useState({
    title: '',
    company: '',
    location: '',
    jd_text: '',
    source_url: '',
  });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);
  const list = useAsync(
    () =>
      jobsApi.list({
        q: filters.q,
        status: filters.status || undefined,
        fee_bearing: filters.fee_bearing === '' ? undefined : filters.fee_bearing === 'true',
        has_warm_path: filters.has_warm_path === '' ? undefined : filters.has_warm_path === 'true',
      }),
    [filters.q, filters.status, filters.fee_bearing, filters.has_warm_path],
  );

  async function create(event) {
    event.preventDefault();
    if (!form.title.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await jobsApi.create(form);
      setForm({ title: '', company: '', location: '', jd_text: '', source_url: '' });
      list.reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  async function onCsv(event) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const text = await file.text();
      const postings = parseCsv(text);
      if (!postings.length) throw new Error('No rows with a title in that CSV.');
      await jobsApi.import({ source: 'csv', postings });
      list.reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  const items = list.data?.items ?? [];

  return (
    <div className="stack">
      <PageHeader
        title="Jobs"
        subtitle="Open-market and mandate roles, ranked against everyone in the CRM."
      />
      <ErrorBox error={error} />

      <form className="card stack" onSubmit={create} data-testid="job-create-form">
        <strong className="small">Add a posting</strong>
        <div className="grid two">
          <input
            type="text"
            placeholder="Title"
            value={form.title}
            onChange={(event) => setForm({ ...form, title: event.target.value })}
          />
          <input
            type="text"
            placeholder="Company"
            value={form.company}
            onChange={(event) => setForm({ ...form, company: event.target.value })}
          />
          <input
            type="text"
            placeholder="Location"
            value={form.location}
            onChange={(event) => setForm({ ...form, location: event.target.value })}
          />
          <input
            type="text"
            placeholder="Source URL"
            value={form.source_url}
            onChange={(event) => setForm({ ...form, source_url: event.target.value })}
          />
        </div>
        <textarea
          rows={4}
          placeholder="Job description"
          value={form.jd_text}
          onChange={(event) => setForm({ ...form, jd_text: event.target.value })}
        />
        <div className="row wrap">
          <button type="submit" className="primary" disabled={busy}>
            Add posting
          </button>
          <button type="button" className="ghost" onClick={() => fileRef.current?.click()} disabled={busy}>
            Import CSV
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            hidden
            onChange={onCsv}
          />
        </div>
      </form>

      <div className="jobs-filters">
        <input
          type="search"
          placeholder="Search title or company"
          value={filters.q}
          onChange={(event) => setFilters({ ...filters, q: event.target.value })}
        />
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
                  </td>
                  <td>{posting.location || '—'}</td>
                  <td>
                    <Badge>{posting.status}</Badge>
                  </td>
                  <td>{posting.match_count}</td>
                  <td className="row wrap">
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
