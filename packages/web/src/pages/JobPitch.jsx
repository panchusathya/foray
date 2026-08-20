import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { Badge } from '../components/common.jsx';
import { jobsApi, sourceLabel, sourceTone } from '../lib/jobs.js';
import { useCurrentProject } from '../lib/project.jsx';

export function SourceBadge({ source }) {
  return (
    <span data-testid="job-source">
      <Badge tone={sourceTone(source)}>{sourceLabel(source)}</Badge>
    </span>
  );
}

export async function openPitchPath(posting, { reload, setProject, navigate }) {
  let projectId = posting.project_id;
  if (!projectId) {
    const project = await jobsApi.createProject(posting.id);
    projectId = project.id;
    await reload();
  }
  setProject(projectId);
  navigate(`/sequences?new=1&project=${encodeURIComponent(projectId)}`);
}

export function PitchButton({ posting, onError, className = 'tiny primary' }) {
  const navigate = useNavigate();
  const { reload, setProject } = useCurrentProject();
  const [busy, setBusy] = useState(false);
  const needsProject = !posting.project_id;

  async function onClick() {
    setBusy(true);
    try {
      await openPitchPath(posting, { reload, setProject, navigate });
    } catch (caught) {
      onError?.(caught);
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      className={className}
      onClick={onClick}
      disabled={busy}
      data-testid="pitch-cta"
    >
      {busy ? 'Working…' : needsProject ? 'Create project to pitch' : 'Open sequences to pitch'}
    </button>
  );
}

export function ProjectLink({ posting }) {
  if (!posting.project_id) return null;
  return (
    <Link to={`/projects/${posting.project_id}`} className="jobs-inline-link">
      Open project
    </Link>
  );
}
