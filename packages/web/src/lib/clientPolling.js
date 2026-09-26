import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Rows that keep themselves current: loaded once, then re-fetched every
 * `intervalMs` while the tab is visible, and again the moment it becomes
 * visible. This is the "sync" between the client's page and ours -- both
 * read the same rows, so a stage the client moves shows up on our side on
 * the next tick without anybody reloading.
 */
export function usePolledRows(load, intervalMs = 30000) {
  const [rows, setRows] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [syncedAt, setSyncedAt] = useState(null);
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      setRows(await load());
      setError(null);
      setSyncedAt(Date.now());
    } catch (caught) {
      setError(caught);
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, [load]);

  useEffect(() => {
    setLoading(true);
    refresh();
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') refresh();
    }, intervalMs);
    const onVisible = () => document.visibilityState === 'visible' && refresh();
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [refresh, intervalMs]);

  return { rows, setRows, error, loading, syncedAt, refresh };
}
