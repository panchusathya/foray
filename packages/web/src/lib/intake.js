/** Intake calls and preferences. Auth wrapper cloned from jobs.js. */

import { ApiError, getToken } from './api.js';

async function request(path, { method = 'GET', body } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

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
    const detail = payload?.detail;
    const message = Array.isArray(detail)
      ? detail.map((d) => `${d.loc?.slice(1).join('.')}: ${d.msg}`).join('; ')
      : detail;
    throw new ApiError(response.status, message ?? payload);
  }
  return payload;
}

function queryString(params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    search.set(key, String(value));
  });
  const suffix = search.toString();
  return suffix ? `?${suffix}` : '';
}

export const intakeApi = {
  listCalls: (params = {}) => request(`/v1/intake/calls${queryString(params)}`),
  countCalls: (params = {}) => request(`/v1/intake/calls/count${queryString(params)}`),
  getCall: (id) => request(`/v1/intake/calls/${id}`),
  patchCall: (id, payload) => request(`/v1/intake/calls/${id}`, { method: 'PATCH', body: payload }),
  listThreads: (params = {}) => request(`/v1/intake/threads${queryString(params)}`),
  countThreads: (params = {}) => request(`/v1/intake/threads/count${queryString(params)}`),
  getThread: (id) => request(`/v1/intake/threads/${id}`),
  patchThread: (id, payload) =>
    request(`/v1/intake/threads/${id}`, { method: 'PATCH', body: payload }),
  getPreference: (candidateId) => request(`/v1/intake/preferences/${candidateId}`),
  confirmPreference: (candidateId) =>
    request(`/v1/intake/preferences/${candidateId}/confirm`, { method: 'POST' }),
};

export function formatTimeline(value) {
  return value ? String(value).replaceAll('_', ' ') : '—';
}

export function formatComp(value) {
  if (value == null || value === '') return '—';
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  if (number >= 1000) return `$${Math.round(number / 1000)}k`;
  return `$${number}`;
}

/** iMessage delivery state for an outbound intake message. SMS stays at Sent. */
export function messageReceipt(message) {
  if (!message || message.direction === 'inbound') return '';
  if (message.read_at) return 'Read';
  if (message.delivered_at) return 'Delivered';
  if (message.status === 'failed') return 'Failed';
  if (message.sent_at || message.status === 'sent' || message.status === 'delivered' || message.status === 'read') {
    return 'Sent';
  }
  return '';
}
