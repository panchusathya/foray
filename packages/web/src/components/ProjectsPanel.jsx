/**
 * Projects, as a panel rather than a page.
 *
 * There is no /projects list any more: a project is the thing you are working
 * *in*, so picking one belongs at the top of the sidebar and the roster
 * belongs on Home next to everything else you glance at. This component is
 * the roster half — create, open, switch, delete.
 *
 * "Open" is a real link, not the status pill. The pill said `open` because
 * that is the project's status, and everyone tried to click it.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, ErrorBox, Loading } from './common.jsx';
import { api } from '../lib/api.js';
import { useCurrentProject } from '../lib/project.jsx';
import { useAsync } from '../lib/useAsync.js';

const BLANK = { name: '', role_title: '', location: '', description: '', company: '' };

export function ProjectsPanel() {
  const projects = useAsync(() => api.listProjects(), []);
  // The sidebar picker reads the shared CurrentProject context, so a create or
  // a delete has to refresh that too — otherwise the dropdown keeps offering a
  // project that is gone until a full reload.
  const { project: current, reload: reloadCurrentProject, setProject } = useCurrentProject();
  const [form, setForm] = useState(BLANK);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function create(event) {
    event.preventDefault();
    if (!form.name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.createProject(form);
      setForm(BLANK);
      setAdding(false);
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
        `Delete “${project.name}”? Pipeline cards and sourcing agents for this role go with it. ` +
          'Sequences stay, but lose the role title.',
      )
    ) {
      return;
    }
    setError(null);
    try {
      await api.deleteProject(project.id);
      // Drop the selection before reloading if it was the one that just went:
      // leaving a dead id in localStorage routes every read through the
      // "?? projects[0]" fallback for good, so the switcher and the stored
      // value never agree again.
      if (current?.id === project.id) setProject(null);
      projects.reload();
      await reloadCurrentProject();
    } catch (caught) {
      setError(caught);
    }
  }

  const rows = projects.data ?? [];

  return (
    <div className="card stack" data-testid="projects-panel">
      <div className="spread">
        <h3 style={{ margin: 0 }}>Projects</h3>
        <button type="button" className="tiny ghost" onClick={() => setAdding((open) => !open)}>
          {adding ? 'Cancel' : 'New project'}
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        One project per role you&apos;re filling. The one in the sidebar scopes the pages under
        it — its pipeline, its contacts, its sequences. Everything else is workspace-wide.
      </p>
      <ErrorBox error={error} />

      {adding ? (
        <form className="stack project-form" onSubmit={create} data-testid="project-create-form">
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
              placeholder="Client / company"
              value={form.company}
              onChange={(event) => setForm({ ...form, company: event.target.value })}
            />
          </div>
          <input
            type="text"
            placeholder="Short description"
            value={form.description}
            onChange={(event) => setForm({ ...form, description: event.target.value })}
          />
          <div>
            <button type="submit" className="primary" disabled={busy}>
              Create project
            </button>
          </div>
        </form>
      ) : null}

      {projects.loading ? <Loading /> : null}
      {!projects.loading && !rows.length ? (
        <div className="muted small">No projects yet — create one to start a pipeline.</div>
      ) : null}

      {rows.map((project) => (
        <div
          key={project.id}
          className={`project-row${current?.id === project.id ? ' current' : ''}`}
          data-testid="project-row"
        >
          <div className="project-row-main">
            <Link to={`/projects/${project.id}`} className="project-row-name">
              {project.name}
            </Link>
            <div className="small muted">
              {project.role_title || 'No role title'}
              {project.location ? ` · ${project.location}` : ''}
              {project.company ? ` · ${project.company}` : ''}
            </div>
          </div>
          <div className="row project-row-actions">
            <Badge tone={project.status === 'open' ? 'strong' : ''}>{project.status}</Badge>
            <Link to={`/projects/${project.id}`} className="tiny-link">
              Open pipeline →
            </Link>
            {current?.id === project.id ? (
              <span className="badge accent">current</span>
            ) : (
              <button type="button" className="tiny ghost" onClick={() => setProject(project.id)}>
                Make current
              </button>
            )}
            <button type="button" className="tiny ghost" onClick={() => remove(project)}>
              Delete
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
