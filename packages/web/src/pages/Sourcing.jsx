import { useState } from 'react';

import { Badge, Empty, ErrorBox, PageHeader } from '../components/common.jsx';
import { foray } from '../lib/foray.js';

const EXAMPLES = [
  'founding ML engineer, shipped training infrastructure, 3-8 years',
  'senior backend engineer in Go with 5+ years',
  'infrastructure platform engineer with kubernetes and terraform',
  'data engineer with spark and airflow',
  'gpu systems engineer writing cuda kernels',
];

/**
 * One sourced candidate. The archetype score is intentionally NOT rendered —
 * Foray shows rank + evidence only. Highlights carry the recommendation.
 */
function ForayHit({ hit }) {
  const [enrich, setEnrich] = useState(null);
  const [enriching, setEnriching] = useState(false);

  const linkedin = enrich?.linkedin?.linkedin_url ?? hit.linkedin_url;
  const badge = enrich?.freshness?.badge;

  async function resolve() {
    setEnriching(true);
    try {
      setEnrich(await foray.enrich(hit.login));
    } catch {
      setEnrich({ error: true });
    } finally {
      setEnriching(false);
    }
  }

  const meta = [
    hit.employer ? `${hit.employer}` : 'employer unknown',
    hit.est_experience_years != null
      ? `~${Number(hit.est_experience_years).toFixed(1)}y (est., ${hit.experience_confidence})`
      : null,
    hit.primary_language,
    hit.location,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <div className="card hit">
      <div className="hit-rank">{hit.rank}</div>
      <div className="hit-body">
        <div className="spread">
          <div>
            <h3>
              <a href={hit.github_url} target="_blank" rel="noreferrer noopener">
                {hit.name || hit.login}
              </a>
            </h3>
            <div className="meta">
              {meta}
              {hit.inferred?.employer ? <span className="badge" style={{ marginLeft: 6 }}>inferred</span> : null}
            </div>
          </div>
        </div>

        {badge ? (
          <p className="small" style={{ color: 'var(--accent-light)', marginBottom: 0 }}>
            {badge}
          </p>
        ) : null}

        {hit.highlights?.length ? (
          <div className="criteria">
            {hit.highlights.map((highlight, index) => (
              <div className="criterion" key={index}>
                <span className="dot strong" />
                <span className="evidence">{highlight}</span>
              </div>
            ))}
          </div>
        ) : null}

        <div className="row wrap" style={{ marginTop: 10 }}>
          {(hit.domain_markers ?? []).map((marker) => (
            <Badge key={marker}>{marker}</Badge>
          ))}
          {(hit.flags ?? []).map((flag) => (
            <Badge key={flag} tone="partial">
              {flag}
            </Badge>
          ))}
        </div>

        {hit.top_repos?.length ? (
          <div className="small muted" style={{ marginTop: 8 }}>
            {hit.top_repos
              .slice(0, 3)
              .map((repo) => `${repo.name} (${repo.stars}★)`)
              .join(' · ')}
          </div>
        ) : null}

        <div className="row" style={{ marginTop: 10 }}>
          <a className="badge" href={hit.github_url} target="_blank" rel="noreferrer noopener">
            GitHub
          </a>
          {linkedin ? (
            <a className="badge" href={linkedin} target="_blank" rel="noreferrer noopener">
              LinkedIn
            </a>
          ) : (
            <button type="button" className="tiny ghost" onClick={resolve} disabled={enriching}>
              {enriching ? 'Resolving…' : 'Resolve LinkedIn / refresh'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export function SourcingPage() {
  const [query, setQuery] = useState(EXAMPLES[0]);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(null);

  async function runSearch(text) {
    const value = (text ?? query).trim();
    if (!value) return;
    setBusy(true);
    setError(null);
    const started = performance.now();
    try {
      const response = await foray.search(value, 40);
      setResult(response);
      setElapsed(Math.round(performance.now() - started));
    } catch (caught) {
      setError(caught);
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <PageHeader
        title="Sourcing"
        subtitle="Search GitHub-sourced engineers by archetype fit. Ranked by evidence, not a number — nothing here calls an external API at query time."
      />

      <div className="card">
        <div className="searchbar">
          <textarea
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="e.g. founding ML engineer in SF, 3-8 years, shipped training infra"
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) runSearch();
            }}
          />
          <button type="button" className="primary" onClick={() => runSearch()} disabled={busy}>
            {busy ? 'Searching…' : 'Search'}
          </button>
        </div>
        <div className="facets">
          <span className="small muted">Try:</span>
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="tiny ghost"
              onClick={() => {
                setQuery(example);
                runSearch(example);
              }}
            >
              {example.slice(0, 42)}…
            </button>
          ))}
        </div>
      </div>

      <ErrorBox error={error} />

      {result ? (
        <>
          <div className="card spread">
            <span className="small muted">
              {result.archetype ? result.archetype.label : 'semantic search'} · parser:{' '}
              {result.parser} · {result.returned} shown of {result.total_matched} matched
              {elapsed != null ? ` · ${elapsed}ms` : ''}
            </span>
            <span className="small muted">display floor {result.display_floor}</span>
          </div>

          {result.message ? <div className="card small">{result.message}</div> : null}

          {result.results.length === 0 ? (
            <Empty>Nothing cleared the bar. Widen the search — drop a filter or lower the experience.</Empty>
          ) : (
            <div className="stack">
              {result.results.map((hit) => (
                <ForayHit key={hit.login} hit={hit} />
              ))}
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}
