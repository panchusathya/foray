import { useEffect, useState } from 'react';

/**
 * The settled value of something that changes per keystroke.
 *
 * A search that re-fires on every character puts eight requests on the wire
 * for an eight-letter name, all racing each other; useAsync keeps the newest
 * from being overwritten, but the requests themselves are still waste, and the
 * list still flickers through prefixes on the way. Waiting for the typing to
 * stop is what makes the list correspond to the box.
 */
export function useDebounced(value, ms = 250) {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    const handle = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(handle);
  }, [value, ms]);

  return settled;
}
