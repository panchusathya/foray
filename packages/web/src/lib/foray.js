/** Client for the Foray sourcing engine API (separate service, proxied at /foray). */

async function forayRequest(path, { method = 'GET', body, signal } = {}) {
  const response = await fetch(`/foray${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    throw new Error(payload?.detail ?? `Foray request failed (${response.status})`);
  }
  return payload;
}

export const foray = {
  health: () => forayRequest('/health'),
  search: (
    query,
    { limit = 40, offset = 0, hasContact = false, signal } = {},
  ) =>
    forayRequest('/search', {
      method: 'POST',
      body: { query, limit, offset, has_contact: hasContact },
      signal,
    }),
  enrich: (login) => forayRequest(`/candidate/${encodeURIComponent(login)}/enrich`),
};
