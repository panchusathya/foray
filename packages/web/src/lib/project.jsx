/**
 * Current-project context. The sidebar's "Current project" section and any
 * page that wants a default project read from here. Selection persists in
 * localStorage and falls back to the first project in the org.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { api } from './api.js';

const STORAGE_KEY = 'foray.project';

const ProjectContext = createContext({
  projects: [],
  project: null,
  loading: true,
  setProject: () => {},
  reload: () => {},
});

export function CurrentProjectProvider({ children }) {
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(() => localStorage.getItem(STORAGE_KEY) ?? '');
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api.listProjects();
      setProjects(list ?? []);
    } catch {
      setProjects([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const setProject = useCallback((id) => {
    setProjectId(id ?? '');
    if (id) localStorage.setItem(STORAGE_KEY, id);
    else localStorage.removeItem(STORAGE_KEY);
  }, []);

  const project = useMemo(() => {
    if (!projects.length) return null;
    return projects.find((p) => p.id === projectId) ?? projects[0];
  }, [projects, projectId]);

  // Write the fallback back. Without this, a stored id that is not in the list
  // -- a deleted project, a project from another account on the same browser --
  // leaves `projectId` and the effective `project` permanently disagreeing, so
  // every consumer that reads the raw id instead of `project.id` silently
  // scopes to the wrong thing. `list_projects` is newest-first, so the
  // fallback is the newest project, not an arbitrary one.
  useEffect(() => {
    if (!loading && project && project.id !== projectId) setProject(project.id);
  }, [loading, project, projectId, setProject]);

  const value = useMemo(
    () => ({ projects, project, loading, setProject, reload }),
    [projects, project, loading, setProject, reload],
  );

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useCurrentProject() {
  return useContext(ProjectContext);
}
