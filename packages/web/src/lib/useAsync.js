import { useCallback, useEffect, useRef, useState } from 'react';

import { newestOnly } from './race.js';

/**
 * Load-once-and-refresh helper. Guards against setting state after unmount so
 * navigating away mid-request doesn't warn or clobber the next page's data,
 * and against an older request reporting after a newer one: deps that change
 * while a request is in flight (a search box, a sort header) otherwise leave
 * whichever call the network happens to finish last on screen, under a label
 * describing the other one.
 */
export function useAsync(loader, deps = []) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const mounted = useRef(true);
  const start = useRef(null);
  if (start.current === null) start.current = newestOnly();

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(() => {
    const isNewest = start.current();
    setState((previous) => ({ ...previous, loading: true, error: null }));
    return Promise.resolve()
      .then(loader)
      .then((data) => {
        if (mounted.current && isNewest()) setState({ data, error: null, loading: false });
        return data;
      })
      .catch((error) => {
        if (mounted.current && isNewest()) setState({ data: null, error, loading: false });
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    run();
  }, [run]);

  return { ...state, reload: run };
}
