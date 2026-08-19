import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { PIPELINE_STAGES, api } from '../lib/api.js';
import { useCurrentProject } from '../lib/project.jsx';
import { useAsync } from '../lib/useAsync.js';

export function ProjectsPage() {
  const projects = useAsync(() => api.listProjects(), []);
  // The sidebar "Current project" section and the Home quick-actions read the
  // shared CurrentProject context, so a new project has to refresh that too —
  // otherwise they keep showing "Create your first project" until a full reload.
  const { reload: reloadCurrentProject, setProject } = useCurrentProject();
  const [form, setForm] = useState({ name: '', role_title: '', location: '', description: '' });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function create(event) {
    event.preventDefault();
    if (!form.name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.createProject(form);
      setForm({ name: '', role_title: '', location: '', description: '' });
      projects.reload();
      await reloadCurrentProject();
      if (created?.id) setProject(created.id);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  async function remove(project) {
    if (
      !window.confirm(
        `Delete “${project.name}”? Pipeline cards and sourcing agents for this role go with it. Sequences stay, but lose the role title.`,
      )
    ) {
      return;
    }
    setError(null);
    try {
      await api.deleteProject(project.id);
      projects.reload();
      await reloadCurrentProject();
    } catch (caught) {
      setError(caught);
    }
  }

  return (
    <div className="stack">
      <PageHeader title="Projects" subtitle="One project per role you're filling." />
      <ErrorBox error={error} />

      <form className="card stack" onSubmit={create}>
        <strong className="small">New project</strong>
        <div className="grid two">
          <input
            type="text"
            placeholder="Project name"
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
          />
          <input
            type="text"
            placeholder="Role title (used in outreach)"
            value={form.role_title}
            onChange={(event) => setForm({ ...form, role_title: event.target.value })}
          />
          <input
            type="text"
            placeholder="Location"
            value={form.location}
            onChange={(event) => setForm({ ...form, location: event.target.value })}
          />
          <input
            type="text"
            placeholder="Short description"
            value={form.description}
            onChange={(event) => setForm({ ...form, description: event.target.value })}
          />
        </div>
        <div>
          <button type="submit" className="primary" disabled={busy}>
            Create project
          </button>
        </div>
      </form>

      {projects.loading ? <Loading /> : null}
      {!projects.loading && !projects.data?.length ? (
        <Empty>No projects yet.</Empty>
      ) : (
        <div className="grid two">
          {(projects.data ?? []).map((project) => (
            <div key={project.id} className="card stack" style={{ gap: 8 }}>
              <div className="spread">
                <Link to={`/projects/${project.id}`} style={{ fontWeight: 600, textDecoration: 'none' }}>
                  {project.name}
                </Link>
                <div className="row">
                  <Badge>{project.status}</Badge>
                  <button type="button" className="tiny ghost" onClick={() => remove(project)}>
                    Delete
                  </button>
                </div>
              </div>
              <div className="small muted">{project.role_title || 'No role title'}</div>
              {project.location ? <div className="small muted">{project.location}</div> : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// "contacted" and "replied" are driven by outreach (a send / a reply), not by
// hand — the backend refuses a bare manual move into them. A recruiter who
// reached out themselves can still record it via an explicit override.
const OUTREACH_DRIVEN_STAGES = new Set(['contacted', 'replied']);

export function ProjectDetailPage() {
  const { projectId } = useParams();
  const pipeline = useAsync(() => api.getPipeline(projectId), [projectId]);
  const funnel = useAsync(() => api.funnel(projectId), [projectId]);
  const sequences = useAsync(() => api.listSequences(), []);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [sequenceId, setSequenceId] = useState('');
  const [enrolling, setEnrolling] = useState(false);
  const [notice, setNotice] = useState('');

  // Sequences for this role first; org-wide ones without a project still work
  // (enroll passes project_id explicitly, so stage moves land in this pipeline).
  const enrollable = (sequences.data ?? []).filter(
    (sequence) => sequence.project_id === projectId || !sequence.project_id,
  );

  function toggleSelected(candidateId) {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(candidateId)) next.delete(candidateId);
      else next.add(candidateId);
      return next;
    });
  }

  async function enrollSelected() {
    const sequence = enrollable.find((s) => s.id === sequenceId) ?? enrollable[0];
    if (!sequence) return;
    const chosen = (pipeline.data ?? []).filter((entry) => selected.has(entry.candidate.id));
    const withoutEmail = chosen.filter((entry) => !(entry.candidate.emails ?? []).length);
    // The sequencer stops an enrollment for an email-less candidate; it retries
    // on a later run once an address is backfilled, but warn up front anyway.
    const lines = [
      `Enroll ${chosen.length} candidate${chosen.length === 1 ? '' : 's'} in “${sequence.name}”?`,
    ];
    if (withoutEmail.length) {
      lines.push(`${withoutEmail.length} of them have no email yet and won't send until one is found.`);
    }
    if (sequence.status !== 'active') {
      lines.push('The sequence is paused — it will be activated so sending can start.');
    }
    if (!window.confirm(lines.join('\n'))) return;

    setEnrolling(true);
    setError(null);
    setNotice('');
    try {
      await api.enroll(sequence.id, {
        candidate_ids: chosen.map((entry) => entry.candidate.id),
        project_id: projectId,
      });
      if (sequence.status !== 'active') {
        await api.setSequenceStatus(sequence.id, 'active');
        sequences.reload();
      }
      setSelected(new Set());
      // Fire what's already due instead of waiting for the next worker tick.
      // Failure here is non-fatal: the worker sends due steps within a minute.
      let sentNow = 0;
      try {
        const run = await api.runSequence(sequence.id);
        sentNow = run?.sent ?? 0;
      } catch {
        /* worker fallback */
      }
      setNotice(
        sentNow
          ? `Enrolled ${chosen.length} in “${sequence.name}” — ${sentNow} email${sentNow === 1 ? '' : 's'} sent.`
          : `Enrolled ${chosen.length} in “${sequence.name}” — due steps send within about a minute.`,
      );
      pipeline.reload();
      funnel.reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setEnrolling(false);
    }
  }

  async function move(entry, stage, { override = false } = {}) {
    setError(null);
    try {
      await api.updateStage(projectId, entry.id, { stage, override });
      pipeline.reload();
      funnel.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  function manualTouch(entry, stage) {
    const label = stage === 'contacted' ? 'contacted' : 'replied';
    const verb = stage === 'contacted' ? 'reached out to' : 'heard back from';
    if (
      window.confirm(
        `Mark ${entry.candidate.full_name} as ${label}? Only do this if you ${verb} them ` +
          `yourself outside a sequence — otherwise it happens automatically.`,
      )
    ) {
      move(entry, stage, { override: true });
    }
  }

  const byStage = PIPELINE_STAGES.map((stage) => ({
    stage,
    entries: (pipeline.data ?? []).filter((entry) => entry.stage === stage),
  }));

  return (
    <div className="stack">
      <PageHeader
        title="Pipeline"
        subtitle="Every card keeps the match evidence that put the person here."
        actions={<Link className="badge" to="/">Back to search</Link>}
      />
      <div className="card small muted">
        Sourced → Contacted → Replied advance automatically as outreach sends and replies land.
        Screening onward you move by hand. Use the small "I reached out" link only when you
        contacted someone yourself outside a sequence.
      </div>
      <ErrorBox error={error} />
      {notice ? <div className="card small">{notice}</div> : null}

      {selected.size > 0 ? (
        <div className="card spread small">
          <span>
            {selected.size} selected
          </span>
          <div className="row">
            {enrollable.length ? (
              <>
                <select
                  style={{ width: 260 }}
                  value={sequenceId || enrollable[0].id}
                  onChange={(event) => setSequenceId(event.target.value)}
                >
                  {enrollable.map((sequence) => (
                    <option key={sequence.id} value={sequence.id}>
                      {sequence.name}
                      {sequence.status !== 'active' ? ' (paused)' : ''}
                    </option>
                  ))}
                </select>
                <button type="button" className="primary tiny" disabled={enrolling} onClick={enrollSelected}>
                  {enrolling ? 'Enrolling…' : 'Enroll in sequence'}
                </button>
              </>
            ) : (
              <Link className="badge" to="/sequences">
                Create a sequence first →
              </Link>
            )}
            <button type="button" className="tiny ghost" onClick={() => setSelected(new Set())}>
              clear
            </button>
          </div>
        </div>
      ) : null}

      {pipeline.loading ? <Loading /> : null}

      {!pipeline.loading && !(pipeline.data ?? []).length ? (
        <Empty>
          Nothing in this pipeline yet — run a search and add people to it.
        </Empty>
      ) : (
        <div className="board">
          {byStage.map(({ stage, entries }) => (
            <div className="column" key={stage}>
              <h4>
                {stage} ({entries.length})
              </h4>
              {entries.map((entry) => {
                const stageIndex = PIPELINE_STAGES.indexOf(entry.stage);
                const next = PIPELINE_STAGES[stageIndex + 1];
                const topCriteria = (entry.rationale?.criteria ?? [])
                  .filter((criterion) => criterion.status === 'strong')
                  .slice(0, 2);
                return (
                  <div className="pipeline-card" key={entry.id}>
                    <label className="row" style={{ gap: 6, alignItems: 'baseline' }}>
                      {entry.stage !== 'rejected' ? (
                        <input
                          type="checkbox"
                          checked={selected.has(entry.candidate.id)}
                          onChange={() => toggleSelected(entry.candidate.id)}
                        />
                      ) : null}
                      <span className="name">{entry.candidate.full_name}</span>
                    </label>
                    <div className="small muted">
                      {entry.candidate.current_title || '—'}
                      {entry.candidate.current_company ? ` · ${entry.candidate.current_company}` : ''}
                    </div>
                    {entry.score ? (
                      <div className="small muted mono">match {(entry.score * 100).toFixed(0)}</div>
                    ) : null}
                    {topCriteria.map((criterion) => (
                      <div className="small muted" key={criterion.name}>
                        ✓ {criterion.label}
                      </div>
                    ))}
                    <div className="row" style={{ marginTop: 8 }}>
                      {next && !OUTREACH_DRIVEN_STAGES.has(next) ? (
                        <button type="button" className="tiny" onClick={() => move(entry, next)}>
                          → {next}
                        </button>
                      ) : null}
                      {next && OUTREACH_DRIVEN_STAGES.has(next) ? (
                        <button
                          type="button"
                          className="tiny ghost"
                          title={`Records a manual touch — ${next} normally happens automatically`}
                          onClick={() => manualTouch(entry, next)}
                        >
                          {next === 'contacted' ? 'I reached out →' : 'Log reply →'}
                        </button>
                      ) : null}
                      {entry.stage !== 'rejected' ? (
                        <button
                          type="button"
                          className="tiny ghost"
                          onClick={() => move(entry, 'rejected')}
                        >
                          reject
                        </button>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}

      {funnel.data ? (
        <div className="grid four">
          <div className="card stat">
            <div className="value">{funnel.data.outreach.sent}</div>
            <div className="label">emails sent</div>
          </div>
          <div className="card stat">
            <div className="value">{(funnel.data.open_rate * 100).toFixed(0)}%</div>
            <div className="label">open rate</div>
          </div>
          <div className="card stat">
            <div className="value">{(funnel.data.reply_rate * 100).toFixed(0)}%</div>
            <div className="label">reply rate</div>
          </div>
          <div className="card stat">
            <div className="value">{(funnel.data.positive_reply_rate * 100).toFixed(0)}%</div>
            <div className="label">positive replies</div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
