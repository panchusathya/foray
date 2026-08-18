/** Typed-ish API client. One place that knows about auth, errors and routes. */

const TOKEN_KEY = 'juicebox.token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) ?? '';
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : (detail?.detail ?? `Request failed (${status})`));
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, { method = 'GET', body, auth = true } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  const token = getToken();
  if (auth && token) headers.Authorization = `Bearer ${token}`;

  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 204) return null;

  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }

  if (!response.ok) {
    // FastAPI validation errors arrive as a list; flatten to something readable.
    const detail = payload?.detail;
    const message = Array.isArray(detail)
      ? detail.map((d) => `${d.loc?.slice(1).join('.')}: ${d.msg}`).join('; ')
      : detail;
    throw new ApiError(response.status, message ?? payload);
  }
  return payload;
}

export const api = {
  health: () => request('/health', { auth: false }),

  signup: (payload) => request('/v1/auth/signup', { method: 'POST', body: payload, auth: false }),
  login: (payload) => request('/v1/auth/login', { method: 'POST', body: payload, auth: false }),
  me: () => request('/v1/auth/me'),

  candidateCount: () => request('/v1/candidates/count'),
  listCandidates: (params = {}) => {
    // Backwards-compatible: accepts a bare query string or {q, limit, offset}.
    const { q = '', limit, offset } = typeof params === 'string' ? { q: params } : params;
    const search = new URLSearchParams();
    if (q) search.set('q', q);
    if (limit != null) search.set('limit', String(limit));
    if (offset != null) search.set('offset', String(offset));
    const suffix = search.toString();
    return request(`/v1/candidates${suffix ? `?${suffix}` : ''}`);
  },
  getCandidate: (id) => request(`/v1/candidates/${id}`),

  search: (payload) => request('/v1/search', { method: 'POST', body: payload }),
  recentSearches: () => request('/v1/searches?limit=10'),
  indexStats: () => request('/v1/search/index-stats'),

  listProjects: () => request('/v1/projects'),
  createProject: (payload) => request('/v1/projects', { method: 'POST', body: payload }),
  getPipeline: (projectId) => request(`/v1/projects/${projectId}/pipeline`),
  addToProject: (projectId, payload) =>
    request(`/v1/projects/${projectId}/candidates`, { method: 'POST', body: payload }),
  updateStage: (projectId, entryId, payload) =>
    request(`/v1/projects/${projectId}/pipeline/${entryId}`, { method: 'PATCH', body: payload }),

  listSequences: () => request('/v1/sequences'),
  createSequence: (payload) => request('/v1/sequences', { method: 'POST', body: payload }),
  getSequence: (id) => request(`/v1/sequences/${id}`),
  updateSequence: (id, payload) => request(`/v1/sequences/${id}`, { method: 'PATCH', body: payload }),
  replaceSteps: (id, steps) => request(`/v1/sequences/${id}/steps`, { method: 'PUT', body: steps }),
  cloneSequence: (id) => request(`/v1/sequences/${id}/clone`, { method: 'POST' }),
  generateSequence: (payload) => request('/v1/sequences/generate', { method: 'POST', body: payload }),
  setSequenceStatus: (id, value) =>
    request(`/v1/sequences/${id}/status?value=${encodeURIComponent(value)}`, { method: 'PATCH' }),
  previewSequence: (sequenceId, payload) =>
    request(`/v1/sequences/${sequenceId}/preview`, { method: 'POST', body: payload }),
  enroll: (sequenceId, payload) =>
    request(`/v1/sequences/${sequenceId}/enroll`, { method: 'POST', body: payload }),
  runSequence: (sequenceId) => request(`/v1/sequences/${sequenceId}/run`, { method: 'POST' }),
  listEnrollments: (sequenceId) => request(`/v1/sequences/${sequenceId}/enrollments`),

  listAgents: () => request('/v1/agents'),
  createAgent: (payload) => request('/v1/agents', { method: 'POST', body: payload }),
  runAgent: (agentId) => request(`/v1/agents/${agentId}/run`, { method: 'POST' }),

  funnel: (projectId) =>
    request(`/v1/analytics/funnel${projectId ? `?project_id=${projectId}` : ''}`),
  activity: () => request('/v1/analytics/activity?limit=25'),
};

export const PIPELINE_STAGES = [
  'sourced',
  'contacted',
  'replied',
  'screening',
  'interviewing',
  'offer',
  'hired',
  'rejected',
];
