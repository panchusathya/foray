/**
 * One copy of the application list for the whole signed-in app.
 *
 * Home, the tracker, the People tab and the nav badge all answer questions
 * about the same list. Fetching it per page meant four requests on every
 * navigation and four chances for two screens to disagree about a status the
 * candidate just changed. This holds it once and hands out a `reload` for the
 * places that mutate it (approve, submit, withdraw).
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { api } from './api.js';
import { NEEDS_YOU } from './applications.js';

const ApplicationsContext = createContext(null);

export function ApplicationsProvider({ children }) {
  const [applications, setApplications] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const data = await api.applications();
      setApplications(data.applications || []);
      setError(null);
    } catch (err) {
      setError(err);
      // Keep whatever we already showed. A transient failure should not blank
      // a tracker somebody is reading.
      setApplications((current) => current ?? []);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Applications move without the candidate touching anything (a package gets
  // prepared, a submission lands), so the list refreshes when the tab is
  // brought back to the front rather than on a timer nobody is watching.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') load();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [load]);

  const value = useMemo(() => {
    const rows = applications || [];
    return {
      applications,
      loading: applications === null,
      error,
      reload: load,
      needsYou: rows.filter((row) => NEEDS_YOU.includes(row.status)).length,
      // Let a detail page publish its fresher copy of one row rather than
      // re-reading the whole list after every action.
      patch: (updated) =>
        setApplications((current) =>
          (current || []).map((row) => (row.id === updated.id ? { ...row, ...updated } : row)),
        ),
    };
  }, [applications, error, load]);

  return <ApplicationsContext.Provider value={value}>{children}</ApplicationsContext.Provider>;
}

export function useApplications() {
  const value = useContext(ApplicationsContext);
  if (!value) throw new Error('useApplications must be used inside ApplicationsProvider');
  return value;
}
