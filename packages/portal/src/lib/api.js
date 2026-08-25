// The portal's API client. Same shape as the recruiter app's, narrowed to
// /v1/portal — the only prefix this origin's nginx proxies.

const TOKEN_KEY = 'juicebox.portal.token';

export function getToken() {
  try {
    return window.localStorage.getItem(TOKEN_KEY) || '';
  } catch {
    // Private mode / storage disabled: the session simply does not persist.
    return '';
  }
}

export function setToken(token) {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* nothing to do: the caller still has the token for this page view */
  }
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

// FastAPI's `detail` is a string for our raises and a list of objects for
// validation errors. Flatten both so a caller never renders "[object Object]".
function flattenDetail(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const first = detail[0];
    if (first && typeof first.msg === 'string') return first.msg;
  }
  return fallback;
}

export async function request(path, { method = 'GET', body, auth = true } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (auth) {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(`/v1/portal${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 204) return null;
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new ApiError(
      flattenDetail(payload && payload.detail, `Request failed (${response.status})`),
      response.status,
    );
  }
  return payload;
}

export const api = {
  exchange: (token) => request('/auth/exchange', { method: 'POST', body: { token }, auth: false }),
  requestCode: (email) =>
    request('/auth/otp/request', { method: 'POST', body: { email }, auth: false }),
  verifyCode: (email, code) =>
    request('/auth/otp/verify', { method: 'POST', body: { email, code }, auth: false }),

  me: () => request('/me'),
  patchProfile: (fields) => request('/profile', { method: 'PATCH', body: fields }),

  feed: ({ q = '', location = '' } = {}) => {
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (location) params.set('location', location);
    const query = params.toString();
    return request(`/feed${query ? `?${query}` : ''}`);
  },
  swipe: (postingId, direction) =>
    request(`/feed/${postingId}/swipe`, { method: 'POST', body: { direction } }),

  authConfig: () => request('/auth/config', { auth: false }),
  googleSignIn: (credential) =>
    request('/auth/google', { method: 'POST', body: { credential }, auth: false }),
  applications: () => request('/applications'),
  application: (id) => request(`/applications/${id}`),
  approve: (id) => request(`/applications/${id}/approve`, { method: 'POST' }),
  confirmSubmitted: (id) => request(`/applications/${id}/confirm-submitted`, { method: 'POST' }),
  // Ask us to apply on their behalf. Only offered when can_auto_submit
  // says pressing it would work; 202 with a queued job, not a result.
  submitForMe: (id) => request(`/applications/${id}/submit`, { method: 'POST' }),
  withdraw: (id) => request(`/applications/${id}/withdraw`, { method: 'POST' }),
  networking: (id) => request(`/applications/${id}/networking`),
  draftIntro: (id, contactCandidateId) =>
    request(`/applications/${id}/networking/draft`, {
      method: 'POST',
      body: { contact_candidate_id: contactCandidateId },
    }),

  documents: () => request('/documents'),
  documentUrl: (id) => request(`/documents/${id}/download-url`, { method: 'POST' }),
};
