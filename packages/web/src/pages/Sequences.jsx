/**
 * Sequences overview: KPI cards, the sequence table, and the create modal
 * (start from scratch / clone existing / generate with AI).
 */

import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';

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
      // The sign-off is a variable like everything else here: it resolves to
      // whichever mailbox actually sends. A hardcoded name means every
      // recruiter who starts from scratch mails candidates as somebody else.
      'through it. Open to a chat this week?\n\nBest,\n{{ sender_first_name }}',
  },
];

function percent(rate) {
  return `${Math.round((rate ?? 0) * 100)}%`;
}

// "Founding ML engineers — Baseten". A clone inherits nothing about where it
// goes any more, but the source's project is still the single most useful
// thing to know about it: the list on this page is filtered by project, so a
// bare name gives no clue that the template belongs to another role.
function describeSequence(sequence, projectsById) {
  const project = projectsById.get(sequence.project_id);
  return project ? `${sequence.name} — ${project.name}` : `${sequence.name} — no project`;
}

function CreateSequenceModal({ projects, sequences, defaultProjectId, onClose }) {
  const navigate = useNavigate();
  const [mode, setMode] = useState(null); // null | 'ai' | 'scratch' | 'clone'
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // Tracked so the sync effect below cannot overwrite a project the user
  // actually chose, only the placeholder it left behind.
  const [projectPicked, setProjectPicked] = useState(false);
  const projectsById = new Map(projects.map((p) => [p.id, p]));
  const [form, setForm] = useState({
    name: '',
    // A sequence is tied to a project (its role fills {{ role_title }} etc.), so
    // default to the current project rather than the "No project" placeholder.
    project_id: defaultProjectId || '',
    prompt: '',
    num_steps: 3,
    // Prefer a sequence from the project we are in: cloning one is usually
    // "another touch on this role", and the org-wide list is newest-first, so
    // the raw first entry is whichever project was worked in most recently.
    clone_id:
      sequences.find((s) => s.project_id && s.project_id === defaultProjectId)?.id
      ?? sequences[0]?.id
      ?? '',
  });

  // This used to be a plain useState initialiser, which runs once: arriving at
  // /sequences?new=1 mounts the modal on the first render, before the project
  // context has resolved, and the empty default then stuck for the life of the
  // modal. The old fallback -- projects[0] -- was worse than empty: it silently
  // picked a project the user was not looking at.
  useEffect(() => {
    if (projectPicked || !defaultProjectId) return;
    setForm((previous) =>
      previous.project_id ? previous : { ...previous, project_id: defaultProjectId },
    );
  }, [defaultProjectId, projectPicked]);

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
        // The project goes with the request. Without it the backend keeps the
        // source's, so cloning another project's sequence as a starting point
        // filed the copy under that project -- invisible on this page, which
        // filters by the one in the sidebar.
        created = await api.cloneSequence(form.clone_id, {
          project_id: form.project_id || null,
        });
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
              <label className="stack" style={{ gap: 4 }}>
                <select
                  value={form.clone_id}
                  onChange={(event) => setForm({ ...form, clone_id: event.target.value })}
                  data-testid="clone-source"
                >
                  {sequences.map((sequence) => (
                    <option key={sequence.id} value={sequence.id}>
                      {describeSequence(sequence, projectsById)}
                    </option>
                  ))}
                </select>
                <span className="small muted">
                  The list is workspace-wide, so each one says which project it belongs to.
                  The copy goes wherever you send it below.
                </span>
              </label>
            ) : null}
            {/* Shown for a clone too. It used to be hidden here, and the copy
                silently inherited the source's project as a result. */}
            <label className="stack" style={{ gap: 4 }}>
              <select
                value={form.project_id}
                onChange={(event) => {
                  setProjectPicked(true);
                  setForm({ ...form, project_id: event.target.value });
                }}
                required
                data-testid="sequence-project"
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

function SequenceRow({ sequence, onToggle, onRemove }) {
  return (
    <div className="card spread" data-testid="sequence-row">
      <div>
        <Link to={`/sequences/${sequence.id}`} style={{ fontWeight: 600 }}>
          {sequence.name}
        </Link>
        <div className="small muted">
          {sequence.steps.length} step{sequence.steps.length === 1 ? '' : 's'} ·{' '}
          {sequence.steps.map((step, i) => (i === 0 ? 'day 0' : `+${step.delay_days}d`)).join(' → ')}
        </div>
      </div>
      <div className="row">
        <Badge tone={sequence.status === 'active' ? 'strong' : ''}>{sequence.status}</Badge>
        <button type="button" className="tiny" onClick={() => onToggle(sequence)}>
          {sequence.status === 'active' ? 'Pause' : 'Resume'}
        </button>
        <Link to={`/sequences/${sequence.id}`}>
          <button type="button" className="tiny">
            Open
          </button>
        </Link>
        <button type="button" className="tiny ghost" onClick={() => onRemove(sequence)}>
          Delete
        </button>
      </div>
    </div>
  );
}

export function SequencesPage() {
  const { projects, project, loading: projectLoading } = useCurrentProject();
  const projectId = project?.id ?? null;

  // Fetched once, unscoped, then filtered below. Switching the sidebar project
  // re-filters synchronously -- a server-side filter would put a loading card
  // between every switch.
  const sequences = useAsync(() => api.listSequences(), []);
  // Hold the funnel until the sidebar's project has resolved. Firing it
  // unscoped first would paint org-wide totals for a frame and then swap them
  // for the smaller scoped ones.
  const funnel = useAsync(
    () => (projectLoading ? Promise.resolve(null) : api.funnel(projectId)),
    [projectLoading, projectId],
  );
  const [searchParams, setSearchParams] = useSearchParams();
  // Read once into state, then stripped from the URL below, and dropped again
  // when the modal closes. All three matter: left in the URL, ?project= wins
  // every later open of the modal and ?new=1 reopens it on a refresh; held in
  // state past the first open, it is exactly as sticky as the URL was, and the
  // picker names one project while the list under it shows another.
  const [pitchProjectId, setPitchProjectId] = useState(() => searchParams.get('project') || '');
  const [creating, setCreating] = useState(() => searchParams.get('new') === '1');
  const [showOrphans, setShowOrphans] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!searchParams.has('new') && !searchParams.has('project')) return;
    const next = new URLSearchParams(searchParams);
    next.delete('new');
    next.delete('project');
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  // /funnel echoes the project_id it answered for. Only trust a payload that
  // names the project selected right now -- that rules out the pre-scope null,
  // the frame of zeros before the refetch starts, and a slow answer for a
  // project we have already switched away from.
  const scoped = funnel.data && funnel.data.project_id === projectId ? funnel.data : null;
  const outreach = scoped?.outreach ?? {};
  const stat = (value) => (scoped ? value ?? 0 : '—');
  const rate = (value) => (scoped ? percent(value) : '—');

  const allSequences = sequences.data ?? [];
  const visibleSequences = projectId
    ? allSequences.filter((sequence) => sequence.project_id === projectId)
    : allSequences;
  // A sequence with no project is filtered out of *every* project, so it reads
  // as lost rather than as misfiled. Said out loud, with the way to see it.
  const orphaned = projectId ? allSequences.filter((sequence) => !sequence.project_id) : [];

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
        <Stat value={stat(outreach.sent)} label="Sent" />
        <Stat value={stat(outreach.opened)} label={`Opened · ${rate(scoped?.open_rate)}`} />
        <Stat value={stat(outreach.replied)} label={`Replied · ${rate(scoped?.reply_rate)}`} />
        <Stat
          value={stat(outreach.positive_replies)}
          label={`Interested · ${rate(scoped?.positive_reply_rate)}`}
        />
      </div>

      {sequences.loading ? <Loading /> : null}
      <ErrorBox error={sequences.error} />
      {!sequences.loading && !visibleSequences.length ? (
        <Empty>No sequences for this project yet — create one to start reaching out.</Empty>
      ) : (
        <div className="stack" style={{ gap: 8 }}>
          {visibleSequences.map((sequence) => (
            <SequenceRow
              key={sequence.id}
              sequence={sequence}
              onToggle={toggleStatus}
              onRemove={remove}
            />
          ))}
        </div>
      )}

      {orphaned.length ? (
        <div className="card stack" style={{ gap: 8 }} data-testid="orphan-sequences">
          <div className="spread">
            <span className="small muted">
              {orphaned.length} sequence{orphaned.length === 1 ? '' : 's'} in no project
              {orphaned.length === 1 ? ' is' : ' are'} hidden from every project view. Open one
              to give it a project.
            </span>
            <button
              type="button"
              className="tiny ghost"
              onClick={() => setShowOrphans((open) => !open)}
              data-testid="orphan-toggle"
            >
              {showOrphans ? 'Hide' : 'Show'}
            </button>
          </div>
          {showOrphans
            ? orphaned.map((sequence) => (
                <SequenceRow
                  key={sequence.id}
                  sequence={sequence}
                  onToggle={toggleStatus}
                  onRemove={remove}
                />
              ))
            : null}
        </div>
      ) : null}

      {creating ? (
        <CreateSequenceModal
          projects={projects}
          sequences={sequences.data ?? []}
          defaultProjectId={pitchProjectId || project?.id}
          onClose={() => {
            setCreating(false);
            // Spent. The pitch link seeds one open; after that the sidebar governs.
            setPitchProjectId('');
          }}
        />
      ) : null}
    </div>
  );
}
