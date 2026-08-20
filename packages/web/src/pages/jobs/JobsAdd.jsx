import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { ErrorBox } from '../../components/common.jsx';
import { jobsApi, parseCsv } from '../../lib/jobs.js';

export function JobsAdd() {
  const [form, setForm] = useState({
    title: '',
    company: '',
    location: '',
    jd_text: '',
    source_url: '',
  });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState(false);
  const fileRef = useRef(null);

  async function create(event) {
    event.preventDefault();
    if (!form.title.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await jobsApi.create(form);
      setForm({ title: '', company: '', location: '', jd_text: '', source_url: '' });
      setCreated(true);
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
      setCreated(true);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <ErrorBox error={error} />
      {created ? (
        <div className="card small">
          Saved.{' '}
          <Link to="/jobs?view=overview" className="jobs-inline-link">
            View it in Overview →
          </Link>
        </div>
      ) : null}
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
    </div>
  );
}
