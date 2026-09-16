import { useState } from 'react';

import { CandidateHit } from '../components/CandidateHit.jsx';
import { Badge, Empty, ErrorBox } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';

const EXAMPLES = [
  'senior machine learning engineers in San Francisco with pytorch and production LLM experience, 5+ years',
  'staff backend engineers at fintech companies who know Go and Kafka',
  'product designers with design systems experience at series b startups, remote ok',
  'data engineers in Berlin with dbt and snowflake, not at agencies',
];

// A paste is compiled differently from a sentence, and the recruiter has to be
// able to see which happened -- it explains why only a few skills are required.
const PARSER_LABEL = {
  llm_jd: 'read as a job description',
  heuristic_jd: 'read as a job description (offline)',
  llm: 'read as a search',
  heuristic: 'read as a search (offline)',
};

function ParsedQuery({ parsed }) {
  const entries = [
    ['Titles', parsed.job_titles],
    ['Seniority', parsed.seniority],
    ['Must have', parsed.must_have_skills],
    ['Nice to have', parsed.nice_to_have_skills],
    ['Excluded titles', parsed.title_exclusions],
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
        <span className="small muted">
          {PARSER_LABEL[parsed.__parser] ?? parsed.__parser}
        </span>
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
  const [onlyAvailable, setOnlyAvailable] = useState(false);

  const projects = useAsync(() => api.listProjects(), []);
  const [projectId, setProjectId] = useState('');

  async function runSearch(text, available) {
    const value = (text ?? query).trim();
    const wantAvailable = available ?? onlyAvailable;
    if (!value) return;
    setBusy(true);
    setError(null);
    setAddState('');
    try {
      const response = await api.search({
        query: value,
        limit: 25,
        only_available: wantAvailable,
      });
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
      <div className={`search-hero${result ? ' compact' : ''}`}>
        <h1>
          Who are you <em className="marker">looking for</em>?
        </h1>
        <p className="muted">
          Describe the hire in plain language, or paste the whole job description. Every
          result shows the evidence behind its score.
        </p>
        <div className="card hero-input">
          <textarea
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Paste a job description, or describe the hire — e.g. staff backend engineers at fintech companies who know Go and Kafka"
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) runSearch();
            }}
          />
          <button type="button" className="primary" onClick={() => runSearch()} disabled={busy}>
            {busy ? 'Searching…' : 'Search'}
          </button>
        </div>
        <label className="small avail-toggle">
          <input
            type="checkbox"
            checked={onlyAvailable}
            onChange={(event) => {
              setOnlyAvailable(event.target.checked);
              if (result) runSearch(undefined, event.target.checked);
            }}
          />{' '}
          Only people on the market
          <span className="muted"> — hides anyone we have not spoken to</span>
        </label>

        <div className="facets hero-examples">
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
        <div className="results-col stack">
          <ParsedQuery parsed={{ ...result.parsed, __parser: result.parser }} />

          <div className="card spread">
            <span className="small muted">
              {result.total_matched} matched · showing {result.results.length} · {result.latency_ms}ms
              {result.reranked ? ' · AI reranked' : ''}
              {onlyAvailable ? ' · on the market only' : ''}
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
              {onlyAvailable
                ? 'Nothing matched. Most people have never had an intake call — untick "Only people on the market" to see them.'
                : 'Nothing matched. Loosen a filter, or bring more people in from Import.'}
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
        </div>
      ) : null}
    </div>
  );
}
