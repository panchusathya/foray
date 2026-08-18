/**
 * GitHub connector — sources engineers from the public API.
 *
 * Only reads data GitHub publishes for a user. Respects the documented rate
 * limits by pausing when the remaining budget hits zero rather than hammering
 * through 403s.
 */

import { config } from '../config.js';
import { log } from '../log.js';

const API = 'https://api.github.com';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function headers() {
  const base = {
    Accept: 'application/vnd.github+json',
    'User-Agent': config.githubUserAgent,
    'X-GitHub-Api-Version': '2022-11-28',
  };
  if (config.githubToken) base.Authorization = `Bearer ${config.githubToken}`;
  return base;
}

async function request(path) {
  const response = await fetch(`${API}${path}`, { headers: headers() });

  const remaining = Number.parseInt(response.headers.get('x-ratelimit-remaining') ?? '', 10);
  if (Number.isFinite(remaining) && remaining <= 1) {
    const resetAt = Number.parseInt(response.headers.get('x-ratelimit-reset') ?? '0', 10) * 1000;
    const waitMs = Math.max(0, resetAt - Date.now()) + 1000;
    log.warn('github rate limit reached, pausing', { waitMs });
    await sleep(waitMs);
  }

  if (!response.ok) {
    throw new Error(`github ${path} failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

function languagesFromRepos(repos) {
  const tally = new Map();
  for (const repo of repos) {
    if (repo.fork || !repo.language) continue;
    tally.set(repo.language, (tally.get(repo.language) ?? 0) + 1 + (repo.stargazers_count ?? 0));
  }
  return [...tally.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([language]) => language);
}

function toCandidateFromProfile(profile, repos) {
  const fullName = (profile.name ?? profile.login ?? '').trim();
  if (!fullName) return null;

  const topRepos = repos
    .filter((repo) => !repo.fork)
    .sort((a, b) => (b.stargazers_count ?? 0) - (a.stargazers_count ?? 0))
    .slice(0, 3);

  const summaryParts = [profile.bio ?? ''].filter(Boolean);
  if (topRepos.length) {
    summaryParts.push(
      `Public work: ${topRepos
        .map((repo) => `${repo.name} (${repo.stargazers_count ?? 0}★${repo.language ? `, ${repo.language}` : ''})`)
        .join('; ')}.`,
    );
  }

  return {
    source: 'github',
    source_id: String(profile.id ?? profile.login),
    full_name: fullName,
    headline: profile.company ? `${profile.bio ?? 'Engineer'} at ${profile.company}` : profile.bio ?? '',
    summary: summaryParts.join(' '),
    location: profile.location ?? '',
    open_to_remote: /remote/i.test(profile.bio ?? ''),
    github_url: profile.html_url ?? '',
    website_url: profile.blog ?? '',
    emails: profile.email ? [profile.email] : [],
    skills: languagesFromRepos(repos),
    experiences: profile.company
      ? [
          {
            company: String(profile.company).replace(/^@/, ''),
            title: profile.bio ?? 'Engineer',
            is_current: true,
            location: profile.location ?? '',
            description: profile.bio ?? '',
          },
        ]
      : [],
    raw: { github_login: profile.login, followers: profile.followers },
  };
}

/**
 * Search GitHub users and yield candidate payloads.
 * @param {string} query e.g. 'language:python location:"San Francisco" followers:>100'
 */
export async function* githubCandidates(query, { limit = 30, withRepos = true } = {}) {
  const perPage = Math.min(limit, 100);
  let fetched = 0;
  let page = 1;

  while (fetched < limit) {
    const search = await request(
      `/search/users?q=${encodeURIComponent(query)}&per_page=${perPage}&page=${page}`,
    );
    const items = search.items ?? [];
    if (items.length === 0) return;

    for (const item of items) {
      if (fetched >= limit) return;
      try {
        const profile = await request(`/users/${item.login}`);
        const repos = withRepos
          ? await request(`/users/${item.login}/repos?per_page=100&sort=pushed`)
          : [];
        const candidate = toCandidateFromProfile(profile, repos);
        if (candidate) {
          fetched += 1;
          yield candidate;
        }
      } catch (error) {
        log.warn('skipping github user', { login: item.login, error: error.message });
      }
    }
    page += 1;
  }
}

export const _internal = { toCandidateFromProfile, languagesFromRepos };
