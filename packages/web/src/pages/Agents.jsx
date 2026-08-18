import { useState } from 'react';

import { Badge, Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';

export function AgentsPage() {
  const agents = useAsync(() => api.listAgents(), []);
  const projects = useAsync(() => api.listProjects(), []);
  const sequences = useAsync(() => api.listSequences(), []);

  const [error, setError] = useState(null);
  const [lastRun, setLastRun] = useState(null);
  const [form, setForm] = useState({
    name: '',
    project_id: '',
    query: '',
    min_score: 0.6,
    max_per_run: 10,
    cadence_minutes: 1440,
    auto_enroll_sequence_id: '',
  });

  async function create(event) {
    event.preventDefault();
    setError(null);
    if (!form.project_id || !form.query.trim()) {
      setError(new Error('Pick a project and write a query'));
      return;
    }
    try {
      await api.createAgent({
        ...form,
        min_score: Number(form.min_score),
        max_per_run: Number(form.max_per_run),
        cadence_minutes: Number(form.cadence_minutes),
        auto_enroll_sequence_id: form.auto_enroll_sequence_id || null,
      });
      setForm({ ...form, name: '', query: '' });
      agents.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  async function run(agentId) {
    setError(null);
    try {
      setLastRun(await api.runAgent(agentId));
      agents.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  return (
    <div className="stack">
      <PageHeader
        title="Sourcing agents"
        subtitle="A saved search that runs on a cadence, adds new matches to a pipeline, and can start outreach automatically."
      />
      <ErrorBox error={error} />

      <form className="card stack" onSubmit={create}>
        <strong className="small">New agent</strong>
        <div className="grid two">
          <input
            type="text"
            placeholder="Agent name"
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
          />
          <select
            value={form.project_id}
            onChange={(event) => setForm({ ...form, project_id: event.target.value })}
          >
            <option value="">Target project…</option>
            {(projects.data ?? []).map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </div>
        <textarea
          placeholder="senior machine learning engineers in San Francisco with pytorch"
          value={form.query}
          onChange={(event) => setForm({ ...form, query: event.target.value })}
        />
        <div className="grid four">
          <label className="small muted">
            min score
            <input
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={form.min_score}
              onChange={(event) => setForm({ ...form, min_score: event.target.value })}
            />
          </label>
          <label className="small muted">
            max per run
            <input
              type="number"
              min="1"
              value={form.max_per_run}
              onChange={(event) => setForm({ ...form, max_per_run: event.target.value })}
            />
          </label>
          <label className="small muted">
            cadence (minutes)
            <input
              type="number"
              min="5"
              value={form.cadence_minutes}
              onChange={(event) => setForm({ ...form, cadence_minutes: event.target.value })}
            />
          </label>
          <label className="small muted">
            auto-enroll
            <select
              value={form.auto_enroll_sequence_id}
              onChange={(event) =>
                setForm({ ...form, auto_enroll_sequence_id: event.target.value })
              }
            >
              <option value="">off</option>
              {(sequences.data ?? []).map((sequence) => (
                <option key={sequence.id} value={sequence.id}>
                  {sequence.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div>
          <button type="submit" className="primary">
            Create agent
          </button>
        </div>
      </form>

      {agents.loading ? <Loading /> : null}
      {!agents.loading && !(agents.data ?? []).length ? (
        <Empty>No agents yet.</Empty>
      ) : (
        <div className="stack">
          {(agents.data ?? []).map((agent) => (
            <div className="card" key={agent.id}>
              <div className="spread">
                <div>
                  <strong>{agent.name}</strong>
                  <div className="small muted">{agent.query}</div>
                  <div className="small muted">
                    min score {agent.min_score} · up to {agent.max_per_run} per run · every{' '}
                    {agent.cadence_minutes} min · {agent.total_added} added so far
                  </div>
                </div>
                <div className="row">
                  <Badge tone={agent.status === 'active' ? 'strong' : ''}>{agent.status}</Badge>
                  <button type="button" className="tiny" onClick={() => run(agent.id)}>
                    Run now
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {lastRun ? (
        <div className="card">
          <strong className="small">Last run</strong>
          <div className="small muted">
            evaluated {lastRun.evaluated} · added {lastRun.added} · enrolled {lastRun.enrolled}
          </div>
          <div className="facets">
            {lastRun.candidates.map((candidate) => (
              <Badge key={candidate.id}>{candidate.full_name}</Badge>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
