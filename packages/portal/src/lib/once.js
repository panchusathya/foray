/**
 * Run a keyed async operation at most once per page load.
 *
 * A magic-link grant is single-use: the server consumes it on the first
 * exchange and 401s every one after. That makes a *second* request actively
 * harmful rather than merely wasteful — the first call spends the grant, and
 * the second call's rejection is what the UI ends up rendering, so a link that
 * worked perfectly reads to the candidate as "expired".
 *
 * Two things fire that second request. React 18 StrictMode mounts effects
 * twice in dev, and a person can double-tap a link in any build. Guarding the
 * state update (an `ignore` flag) does not help: both requests still go out.
 * Guarding the *request* does — sharing one promise per key makes every caller
 * after the first a reader of the original result instead of a new attempt.
 *
 * Rejections are cached deliberately. Once an exchange has failed we cannot
 * tell whether the server consumed the grant before failing, so a retry is not
 * safe; the honest recovery is the OTP path, which is what the UI offers.
 */
const inflight = new Map();

export function once(key, factory) {
  let pending = inflight.get(key);
  if (pending === undefined) {
    pending = factory();
    inflight.set(key, pending);
  }
  return pending;
}

/** Test seam. Nothing in the app clears these — a page load is the reset. */
export function resetOnce() {
  inflight.clear();
}
