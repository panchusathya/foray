import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Load-once-and-refresh helper. Guards against setting state after unmount so
 * navigating away mid-request doesn't warn or clobber the next page's data.
 */
export function useAsync(loader, deps = []) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(() => {
    setState((previous) => ({ ...previous, loading: true, error: null }));
    return Promise.resolve()
      .then(loader)
      .then((data) => {
        if (mounted.current) setState({ data, error: null, loading: false });
        return data;
      })
      .catch((error) => {
        if (mounted.current) setState({ data: null, error, loading: false });
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    run();
  }, [run]);

  return { ...state, reload: run };
}
