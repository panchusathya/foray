/**
 * Monotonic request guard, the pattern Sourcing and the sequence editor
 * already hand-roll inline.
 *
 * Two requests for the same thing can be in flight at once whenever the input
 * changes faster than the network answers, and they resolve in whatever order
 * the network gives them -- so without this the SLOWER one wins. On the intake
 * attribution search that means the radio list shows results for a shorter
 * prefix while the box reads the longer one, and the call gets bound to the
 * wrong candidate.
 */
export function newestOnly() {
  let issued = 0;
  // Call at the start of a request; the returned predicate says whether this
  // request is still the newest one, i.e. whether it is allowed to report.
  return function start() {
    const seq = (issued += 1);
    return () => seq === issued;
  };
}
