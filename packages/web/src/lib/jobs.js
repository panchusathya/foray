/** Job postings and matches. Auth wrapper cloned from api.js — not from foray.js. */

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

export const jobsApi = {
  list: (params = {}) => request(`/v1/job-postings${queryString(params)}`),
  get: (id) => request(`/v1/job-postings/${id}`),
  create: (payload) => request('/v1/job-postings', { method: 'POST', body: payload }),
  import: (payload) => request('/v1/job-postings/import', { method: 'POST', body: payload }),
  patch: (id, payload) => request(`/v1/job-postings/${id}`, { method: 'PATCH', body: payload }),
  matches: (id, minScore) =>
    request(`/v1/job-postings/${id}/matches${queryString({ min_score: minScore })}`),
  rematch: (id) => request(`/v1/job-postings/${id}/rematch`, { method: 'POST' }),
  createProject: (id, payload = {}) =>
    request(`/v1/job-postings/${id}/create-project`, { method: 'POST', body: payload }),
  forCandidate: (candidateId) =>
    request(`/v1/job-matches${queryString({ candidate_id: candidateId })}`),
  patchMatch: (id, payload) => request(`/v1/job-matches/${id}`, { method: 'PATCH', body: payload }),
  rematchQueue: () => request('/v1/job-matches/rematch-queue'),
  rematchCandidate: (candidateId) =>
    request(`/v1/job-matches/rematch${queryString({ candidate_id: candidateId })}`, {
      method: 'POST',
    }),
  discoverTurn: (payload) => request('/v1/job-discovery/turns', { method: 'POST', body: payload }),
};

export const JOB_SOURCE_LABELS = {
  exa: 'Exa',
  project: 'Mandate',
  manual: 'Manual',
  csv: 'CSV',
};

export function sourceLabel(source) {
  return JOB_SOURCE_LABELS[source] || source || 'Unknown';
}

export function sourceTone(source) {
  if (source === 'exa') return 'partial';
  if (source === 'project') return 'accent';
  return '';
}

export function parseCsv(text) {
  const lines = text.split(/\r?\n/).filter((line) => line.trim());
  if (!lines.length) return [];
  const headers = splitCsvLine(lines[0]).map((h) => h.trim().toLowerCase());
  return lines.slice(1).map((line) => {
    const cells = splitCsvLine(line);
    const row = {};
    headers.forEach((header, index) => {
      row[header] = cells[index] ?? '';
    });
    return {
      title: row.title || row.role || '',
      company: row.company || '',
      location: row.location || '',
      jd_text: row.jd_text || row.description || row.jd || '',
      source_url: row.source_url || row.url || '',
      seniority: row.seniority || '',
    };
  }).filter((row) => row.title);
}

function splitCsvLine(line) {
  const out = [];
  let current = '';
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (ch === '"') {
      if (quoted && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        quoted = !quoted;
      }
    } else if (ch === ',' && !quoted) {
      out.push(current);
      current = '';
    } else {
      current += ch;
    }
  }
  out.push(current);
  return out;
}
