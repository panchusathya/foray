import { useEffect, useState } from 'react';

import { Badge, Empty, ErrorBox, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { foray } from '../lib/foray.js';
import { forayHitToCandidate, partitionByEmail } from '../lib/forayImport.js';

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
function ContactChip({ hit }) {
  const primary = (hit.emails ?? [])[0];
  if (primary) {
    // Show the SOURCE, never "verified" unless the flag is set — a recruiter
    // must be able to tell a published profile address from a mined one.
    const label = primary.verified ? `email · ${primary.source} ✓` : `email · ${primary.source}`;
    return <Badge tone="strong">{label}</Badge>;
  }
  if (hit.linkedin_url) return <Badge>LinkedIn only</Badge>;
  return <Badge tone="partial">no email</Badge>;
}

function ForayHit({ hit, selected, onToggle }) {
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
      <div className="hit-rank">
        <input
          type="checkbox"
          checked={selected}
          onChange={() => onToggle(hit)}
          aria-label={`Select ${hit.login}`}
        />
        <div style={{ marginTop: 6 }}>{hit.rank}</div>
      </div>
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
          <ContactChip hit={hit} />
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
  const [contactableOnly, setContactableOnly] = useState(false);

  const [selected, setSelected] = useState(new Map()); // login -> hit
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState('');
  const [addState, setAddState] = useState('');

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => setProjects([]));
  }, []);

  function toggle(hit) {
    setSelected((previous) => {
      const next = new Map(previous);
      if (next.has(hit.login)) next.delete(hit.login);
      else next.set(hit.login, hit);
      return next;
    });
  }

  async function runSearch(text) {
    const value = (text ?? query).trim();
    if (!value) return;
    setBusy(true);
    setError(null);
    setAddState('');
    const started = performance.now();
    try {
      const response = await foray.search(value, 40, contactableOnly);
      setResult(response);
      setElapsed(Math.round(performance.now() - started));
      setSelected(new Map());
    } catch (caught) {
      setError(caught);
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  async function importSelected(hits) {
    if (!hits.length) return;
    setAddState('saving');
    setError(null);
    try {
      const archetypeId = result?.archetype?.id ?? null;
      const payload = { candidates: hits.map((h) => forayHitToCandidate(h, { archetypeId })) };
      const { ids } = await api.bulkCandidates(payload);
      if (projectId && ids?.length) {
        await api.addToProject(projectId, { candidate_ids: ids, search_id: null });
      }
      setAddState(
        `Imported ${ids?.length ?? hits.length}${projectId ? ' and added to the project' : ''}.`,
      );
      setSelected(new Map());
    } catch (caught) {
      setError(caught);
      setAddState('');
    }
  }

  function onAddClick() {
    const hits = [...selected.values()];
    if (!hits.length) return;
    const { withEmail, withoutEmail } = partitionByEmail(hits);
    if (withoutEmail.length === 0) {
      importSelected(hits);
      return;
    }
    // The sequencer permanently kills an enrollment for an email-less candidate
    // (even after an address is found later), so make the consequence explicit.
    const ok = window.confirm(
      `${withoutEmail.length} of ${hits.length} selected have no email. If you enroll them in ` +
        `a sequence, that step fails permanently — even after you find their address later.\n\n` +
        `OK = import only the ${withEmail.length} with an email.\n` +
        `Cancel = import all ${hits.length} anyway (they'll sit in the project uncontactable).`,
    );
    importSelected(ok ? withEmail : hits);
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
        <label className="small muted" style={{ display: 'flex', gap: 6, marginTop: 8 }}>
          <input
            type="checkbox"
            checked={contactableOnly}
            onChange={(event) => setContactableOnly(event.target.checked)}
          />
          Contactable only (has an email or LinkedIn)
        </label>
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

          {result.results.length > 0 ? (
            <div className="card spread">
              <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
                  <option value="">No project (import only)</option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="primary"
                  onClick={onAddClick}
                  disabled={selected.size === 0 || addState === 'saving'}
                >
                  {addState === 'saving'
                    ? 'Importing…'
                    : (() => {
                        const hits = [...selected.values()];
                        const missing = partitionByEmail(hits).withoutEmail.length;
                        return `Add ${hits.length}${missing ? ` (${missing} without email)` : ''}`;
                      })()}
                </button>
              </div>
              {addState && addState !== 'saving' ? (
                <span className="small muted">{addState}</span>
              ) : null}
            </div>
          ) : null}

          {result.results.length === 0 ? (
            <Empty>Nothing cleared the bar. Widen the search — drop a filter or lower the experience.</Empty>
          ) : (
            <div className="stack">
              {result.results.map((hit) => (
                <ForayHit
                  key={hit.login}
                  hit={hit}
                  selected={selected.has(hit.login)}
                  onToggle={toggle}
                />
              ))}
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}
