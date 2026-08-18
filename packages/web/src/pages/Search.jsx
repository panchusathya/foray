import { useState } from 'react';

import { CandidateHit } from '../components/CandidateHit.jsx';
import { Badge, Empty, ErrorBox, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';

const EXAMPLES = [
  'senior machine learning engineers in San Francisco with pytorch and production LLM experience, 5+ years',
  'staff backend engineers at fintech companies who know Go and Kafka',
  'product designers with design systems experience at series b startups, remote ok',
  'data engineers in Berlin with dbt and snowflake, not at agencies',
];

function ParsedQuery({ parsed }) {
  const entries = [
    ['Titles', parsed.job_titles],
    ['Seniority', parsed.seniority],
    ['Must have', parsed.must_have_skills],
    ['Nice to have', parsed.nice_to_have_skills],
    ['Locations', parsed.locations],
    ['Industries', parsed.industries],
    ['Companies', parsed.target_companies],
    ['Stages', parsed.company_stages],
    ['Keywords', parsed.keywords],
    ['Excluded', parsed.negative_keywords],
  ].filter(([, values]) => values?.length);

  const years =
    parsed.min_years_experience != null || parsed.max_years_experience != null
      ? `${parsed.min_years_experience ?? 0}–${parsed.max_years_experience ?? '∞'} yrs`
      : null;

  return (
    <div className="card">
      <div className="spread" style={{ marginBottom: 8 }}>
        <strong className="small">Interpreted as</strong>
        <span className="small muted">parser: {parsed.__parser}</span>
      </div>
      <div className="facets">
        {years ? <Badge tone="accent">{years}</Badge> : null}
        {parsed.remote_ok ? <Badge tone="accent">remote ok</Badge> : null}
        {entries.map(([label, values]) => (
          <Badge key={label}>
            {label}: {values.join(', ')}
          </Badge>
        ))}
        {!entries.length && !years ? <span className="muted small">no structured filters</span> : null}
      </div>
    </div>
  );
}

export function SearchPage() {
  const [query, setQuery] = useState(EXAMPLES[0]);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [addState, setAddState] = useState('');

  const projects = useAsync(() => api.listProjects(), []);
  const [projectId, setProjectId] = useState('');

  async function runSearch(text) {
    const value = (text ?? query).trim();
    if (!value) return;
    setBusy(true);
    setError(null);
    setAddState('');
    try {
      const response = await api.search({ query: value, limit: 25 });
      setResult(response);
      setSelected(new Set());
    } catch (caught) {
      setError(caught);
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  function toggle(candidateId) {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(candidateId)) next.delete(candidateId);
      else next.add(candidateId);
      return next;
    });
  }

  async function addSelected() {
    if (!projectId || selected.size === 0) return;
    setAddState('saving');
    try {
      await api.addToProject(projectId, {
        candidate_ids: [...selected],
        search_id: result?.search_id ?? null,
      });
      setAddState(`Added ${selected.size} to the pipeline`);
      setSelected(new Set());
    } catch (caught) {
      setError(caught);
      setAddState('');
    }
  }

  return (
    <div className="stack">
      <PageHeader
        title="Search"
        subtitle="Describe who you're looking for. Every result shows the evidence behind its score."
      />

      <div className="card">
        <div className="searchbar">
          <textarea
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="e.g. staff backend engineers at fintech companies who know Go and Kafka"
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
              {example.slice(0, 44)}…
            </button>
          ))}
        </div>
      </div>

      <ErrorBox error={error} />

      {result ? (
        <>
          <ParsedQuery parsed={{ ...result.parsed, __parser: result.parser }} />

          <div className="card spread">
            <span className="small muted">
              {result.total_matched} matched · showing {result.results.length} · {result.latency_ms}ms
              {result.reranked ? ' · AI reranked' : ''}
            </span>
            <div className="row">
              <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
                <option value="">Add selected to project…</option>
                {(projects.data ?? []).map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={addSelected}
                disabled={!projectId || selected.size === 0 || addState === 'saving'}
              >
                Add {selected.size || ''}
              </button>
            </div>
          </div>

          {addState && addState !== 'saving' ? (
            <div className="card small">{addState}</div>
          ) : null}

          {result.results.length === 0 ? (
            <Empty>
              Nothing matched. Loosen a filter — or import more people with{' '}
              <code className="mono">juicebox seed</code>.
            </Empty>
          ) : (
            <div className="stack">
              {result.results.map((hit) => (
                <CandidateHit
                  key={hit.candidate.id}
                  hit={hit}
                  selected={selected.has(hit.candidate.id)}
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
