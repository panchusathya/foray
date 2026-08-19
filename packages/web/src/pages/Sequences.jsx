/**
 * Sequences overview: KPI cards, the sequence table, and the create modal
 * (start from scratch / clone existing / generate with AI).
 */

import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading, PageHeader, Stat } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useCurrentProject } from '../lib/project.jsx';
import { useAsync } from '../lib/useAsync.js';

const STARTER_STEPS = [
  {
    delay_days: 0,
    subject_template: '{{ subject_hint | A quick note }}',
    body_template:
      'Hi {{ first_name }},\n\n{{ opening_line }}\n\n{{ why_relevant }}\n\n' +
      "We're hiring a {{ role_title | senior engineer }} and I'd love 15 minutes to walk you " +
      'through it. Open to a chat this week?\n\nBest,\nTushar',
  },
];

function percent(rate) {
  return `${Math.round((rate ?? 0) * 100)}%`;
}

function CreateSequenceModal({ projects, sequences, defaultProjectId, onClose }) {
  const navigate = useNavigate();
  const [mode, setMode] = useState(null); // null | 'ai' | 'scratch' | 'clone'
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [form, setForm] = useState({
    name: '',
    // A sequence is tied to a project (its role fills {{ role_title }} etc.), so
    // default to the current project rather than the "No project" placeholder.
    project_id: defaultProjectId || projects[0]?.id || '',
    prompt: '',
    num_steps: 3,
    clone_id: sequences[0]?.id ?? '',
  });

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      let created;
      if (mode === 'scratch') {
        created = await api.createSequence({
          name: form.name || 'New sequence',
          project_id: form.project_id || null,
          steps: STARTER_STEPS,
        });
      } else if (mode === 'clone') {
        created = await api.cloneSequence(form.clone_id);
      } else {
        const generated = await api.generateSequence({
          prompt: form.prompt,
          project_id: form.project_id || null,
          num_steps: Number(form.num_steps),
        });
        created = await api.createSequence(generated.draft);
      }
      navigate(`/sequences/${created.id}`);
    } catch (caught) {
      setError(caught);
      setBusy(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal card stack" onClick={(event) => event.stopPropagation()}>
        <div className="spread">
          <h3 style={{ margin: 0 }}>Create sequence</h3>
          <button type="button" className="tiny ghost" onClick={onClose}>
            Close
          </button>
        </div>

        {!projects.length ? (
          <div className="stack" style={{ gap: 10 }}>
            <p className="small muted" style={{ margin: 0 }}>
              A sequence personalises around a project&apos;s role, so create a project first.
            </p>
            <button
              type="button"
              className="primary"
              onClick={() => {
                onClose();
                navigate('/projects');
              }}
            >
              Create a project
            </button>
          </div>
        ) : mode === null ? (
          <div className="stack" style={{ gap: 8 }}>
            <button type="button" className="option" onClick={() => setMode('ai')}>
              ✦ Generate with AI
              <span className="muted small"> — draft a sequence from a short brief</span>
            </button>
            <button type="button" className="option" onClick={() => setMode('scratch')}>
              + Start from scratch
            </button>
            <button
              type="button"
              className="option"
              disabled={!sequences.length}
              onClick={() => setMode('clone')}
            >
              ⧉ Clone an existing sequence
            </button>
          </div>
        ) : (
          <form className="stack" onSubmit={submit}>
            <ErrorBox error={error} />
            {mode === 'scratch' ? (
              <input
                type="text"
                placeholder="Sequence name"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
              />
            ) : null}
            {mode === 'ai' ? (
              <>
                <textarea
                  placeholder="Who are you reaching out to, and about what? e.g. Founding ML engineers for an Accel-backed robotics startup"
                  value={form.prompt}
                  onChange={(event) => setForm({ ...form, prompt: event.target.value })}
                  rows={3}
                  required
                />
                <label className="small muted">
                  Steps{' '}
                  <select
                    style={{ width: 80, display: 'inline-block' }}
                    value={form.num_steps}
                    onChange={(event) => setForm({ ...form, num_steps: event.target.value })}
                  >
                    <option value={2}>2</option>
                    <option value={3}>3</option>
                    <option value={4}>4</option>
                  </select>
                </label>
              </>
            ) : null}
            {mode === 'clone' ? (
              <select
                value={form.clone_id}
                onChange={(event) => setForm({ ...form, clone_id: event.target.value })}
              >
                {sequences.map((sequence) => (
                  <option key={sequence.id} value={sequence.id}>
                    {sequence.name}
                  </option>
                ))}
              </select>
            ) : null}
            {mode !== 'clone' ? (
              <label className="stack" style={{ gap: 4 }}>
                <select
                  value={form.project_id}
                  onChange={(event) => setForm({ ...form, project_id: event.target.value })}
                  required
                >
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
                <span className="small muted">
                  {"The project's role fills {{ role_title }} in your templates."}
                </span>
              </label>
            ) : null}
            <div className="row">
              <button type="submit" className="primary" disabled={busy}>
                {busy ? 'Working…' : mode === 'ai' ? 'Generate' : mode === 'clone' ? 'Clone' : 'Create'}
              </button>
              <button type="button" className="ghost" onClick={() => setMode(null)}>
                Back
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

export function SequencesPage() {
  const sequences = useAsync(() => api.listSequences(), []);
  const funnel = useAsync(() => api.funnel(), []);
  const { projects, project } = useCurrentProject();
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState(null);

  const outreach = funnel.data?.outreach ?? {};

  async function toggleStatus(sequence) {
    setError(null);
    try {
      await api.setSequenceStatus(sequence.id, sequence.status === 'active' ? 'paused' : 'active');
      sequences.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  async function remove(sequence) {
    if (
      !window.confirm(
        `Delete “${sequence.name}”? Enrolments and sent-message history for this sequence go with it.`,
      )
    ) {
      return;
    }
    setError(null);
    try {
      await api.deleteSequence(sequence.id);
      sequences.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  return (
    <div className="stack">
      <PageHeader
        title="Sequences"
        subtitle="Multi-step outreach. Variables personalise every send; replies and bounces stop the sequence automatically."
        actions={
          <button type="button" className="primary" onClick={() => setCreating(true)}>
            New sequence
          </button>
        }
      />
      <ErrorBox error={error} />

      <div className="grid four">
        <Stat value={outreach.sent ?? 0} label="Sent" />
        <Stat value={outreach.opened ?? 0} label={`Opened · ${percent(funnel.data?.open_rate)}`} />
        <Stat value={outreach.replied ?? 0} label={`Replied · ${percent(funnel.data?.reply_rate)}`} />
        <Stat
          value={outreach.positive_replies ?? 0}
          label={`Interested · ${percent(funnel.data?.positive_reply_rate)}`}
        />
      </div>

      {sequences.loading ? <Loading /> : null}
      <ErrorBox error={sequences.error} />
      {!sequences.loading && !(sequences.data ?? []).length ? (
        <Empty>No sequences yet — create one to start reaching out.</Empty>
      ) : (
        <div className="stack" style={{ gap: 8 }}>
          {(sequences.data ?? []).map((sequence) => (
            <div className="card spread" key={sequence.id}>
              <div>
                <Link to={`/sequences/${sequence.id}`} style={{ fontWeight: 600 }}>
                  {sequence.name}
                </Link>
                <div className="small muted">
                  {sequence.steps.length} step{sequence.steps.length === 1 ? '' : 's'} ·{' '}
                  {sequence.steps
                    .map((step, i) => (i === 0 ? 'day 0' : `+${step.delay_days}d`))
                    .join(' → ')}
                </div>
              </div>
              <div className="row">
                <Badge tone={sequence.status === 'active' ? 'strong' : ''}>{sequence.status}</Badge>
                <button type="button" className="tiny" onClick={() => toggleStatus(sequence)}>
                  {sequence.status === 'active' ? 'Pause' : 'Resume'}
                </button>
                <Link to={`/sequences/${sequence.id}`}>
                  <button type="button" className="tiny">
                    Open
                  </button>
                </Link>
                <button type="button" className="tiny ghost" onClick={() => remove(sequence)}>
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {creating ? (
        <CreateSequenceModal
          projects={projects}
          sequences={sequences.data ?? []}
          defaultProjectId={project?.id}
          onClose={() => setCreating(false)}
        />
      ) : null}
    </div>
  );
}
