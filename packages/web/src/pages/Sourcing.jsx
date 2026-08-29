import { useEffect, useRef, useState } from 'react';

import { Empty, ErrorBox } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { foray } from '../lib/foray.js';
import { forayHitToCandidate } from '../lib/forayImport.js';
import {
  countOnPage,
  pageWindow,
  toggleOne,
  togglePage,
  toggleRange,
} from '../lib/selection.js';

const PAGE_SIZE = 40;

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
function ForayHit({ hit, selected, onToggle, selectionDisabled = false }) {
  const linkedin = hit.linkedin_url;

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

  const highlights = hit.evidence?.highlights ?? hit.highlights ?? [];
  // GitHub first where we have it, then LinkedIn -- a LinkedIn-sourced person
  // used to get a dead plain-text name, which is the one profile link that
  // matters for them.
  const nameHref = hit.github_url || linkedin || null;

  return (
    <div className="card hit">
      <div className="hit-rank">
        <input
          type="checkbox"
          checked={selected}
          disabled={selectionDisabled}
          onClick={(event) => {
            event.preventDefault();
            onToggle(event);
          }}
          onChange={() => {}}
          aria-label={`Select ${hit.login}`}
        />
        <div style={{ marginTop: 6 }}>{hit.rank}</div>
      </div>
      <div className="hit-body">
        <div className="spread">
          <div>
            <h3>
              {nameHref ? (
                <a href={nameHref} target="_blank" rel="noreferrer noopener">
                  {hit.name || hit.login}
                </a>
              ) : (
                <span>{hit.name || hit.login}</span>
              )}
            </h3>
            <div className="meta">{meta}</div>
          </div>
        </div>

        {highlights.length ? (
          <div className="criteria">
            {highlights.map((highlight, index) => (
              <div className="criterion" key={index}>
                <span className="dot strong" />
                <span className="evidence">{highlight}</span>
              </div>
            ))}
          </div>
        ) : null}

        {hit.top_repos?.length ? (
          <div className="small muted" style={{ marginTop: 8 }}>
            {hit.top_repos
              .slice(0, 3)
              .map((repo) => `${repo.name} (${repo.stars}★)`)
              .join(' · ')}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function SourcingPage() {
  const [query, setQuery] = useState(EXAMPLES[0]);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [contactableOnly, setContactableOnly] = useState(false);
  const [activeQuery, setActiveQuery] = useState(null);
  const [activeContactable, setActiveContactable] = useState(false);
  const [page, setPage] = useState(0);
  const [nonce, setNonce] = useState(0);

  const [selected, setSelected] = useState(new Map()); // login -> hit
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState('');
  const [addState, setAddState] = useState('');
  const [showSkeleton, setShowSkeleton] = useState(false);
  const lastIndex = useRef(null);
  const searchRequest = useRef(0);
  // The server's page stride, learned from the first response. PAGE_SIZE is
  // only the pre-first-response default; page 0 asks for offset 0 either way.
  const pageStride = useRef(PAGE_SIZE);

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => setProjects([]));
  }, []);

  useEffect(() => {
    if (!busy || result) {
      setShowSkeleton(false);
      return undefined;
    }
    const timer = setTimeout(() => setShowSkeleton(true), 400);
    return () => clearTimeout(timer);
  }, [busy, result]);

  useEffect(() => {
    if (!activeQuery) return undefined;

    const controller = new AbortController();
    const request = ++searchRequest.current;
    setBusy(true);
    setError(null);

    foray
      .search(activeQuery, {
        // limit 0 means "the server's own page size". Sending a hardcoded 40
        // while the server numbers pages in FORAY_RESULT_LIMIT units would
        // desynchronise the pager from the rows it is paging through.
        limit: 0,
        offset: page * pageStride.current,
        hasContact: activeContactable,
        signal: controller.signal,
      })
      .then((response) => {
        if (request !== searchRequest.current) return;
        pageStride.current = response.page_size || PAGE_SIZE;
        setResult(response);
        lastIndex.current = null;
      })
      .catch((caught) => {
        if (caught?.name !== 'AbortError' && request === searchRequest.current) {
          setError(caught);
        }
      })
      .finally(() => {
        if (request === searchRequest.current) setBusy(false);
      });

    return () => controller.abort();
  }, [activeQuery, activeContactable, page, nonce]);

  function toggle(hit, index, event) {
    const hits = result?.results ?? [];
    if (event?.shiftKey && lastIndex.current != null && hits.length) {
      setSelected((previous) => toggleRange(previous, hits, lastIndex.current, index));
    } else {
      setSelected((previous) => toggleOne(previous, hit));
      lastIndex.current = index;
    }
  }

  function selectPage() {
    const hits = result?.results ?? [];
    setSelected((previous) => togglePage(previous, hits));
  }

  function runSearch(text) {
    const value = (text ?? query).trim();
    if (!value) return;
    searchRequest.current += 1;
    setError(null);
    setAddState('');
    setActiveQuery(value);
    setActiveContactable(contactableOnly);
    setPage(0);
    setNonce((value) => value + 1);
    setSelected(new Map());
    lastIndex.current = null;
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
    // Import the whole selection. This matches what the old confirm dialog's
    // Cancel branch did, so nothing the recruiter ticked is dropped on their
    // behalf.
    importSelected(hits);
  }

  const pageHits = result?.results ?? [];
  const selectedOnPage = countOnPage(selected, pageHits);
  const pageIndexes = result?.page_count <= 12
    ? pageWindow(page, result?.page_count ?? 0, 12)
    : [];

  return (
    <div className="stack">
      <div className={`search-hero${result || showSkeleton ? ' compact' : ''}`}>
        <h1>
          Who should we <em className="marker">source</em>?
        </h1>
        <p className="muted">
          GitHub-sourced engineers by archetype fit. Ranked by evidence, not a number — nothing
          here calls an external API at query time.
        </p>
        <div className="card hero-input">
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
              {example.slice(0, 42)}…
            </button>
          ))}
        </div>
        <label
          className="small muted"
          style={{ display: 'inline-flex', gap: 6, marginTop: 8, justifyContent: 'center' }}
        >
          <input
            type="checkbox"
            checked={contactableOnly}
            onChange={(event) => setContactableOnly(event.target.checked)}
          />
          Contactable only (has an email or LinkedIn)
        </label>
      </div>

      <ErrorBox error={error} />

      {showSkeleton ? (
        <div className="card small muted" data-testid="search-skeleton">
          Searching…
        </div>
      ) : null}

      {result ? (
        <div className="results-col stack" style={{ opacity: busy ? 0.55 : 1 }} aria-busy={busy}>
          {result.results.length > 0 ? (
            <div className="card spread">
              <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                <label className="small muted" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <input
                    type="checkbox"
                    checked={
                      result.results.length > 0 &&
                      result.results.every((hit) => selected.has(hit.login))
                    }
                    disabled={busy}
                    ref={(node) => {
                      if (node) {
                        const some = result.results.some((hit) => selected.has(hit.login));
                        const all = result.results.every((hit) => selected.has(hit.login));
                        node.indeterminate = some && !all;
                      }
                    }}
                    onChange={selectPage}
                    aria-label="Select all on this page"
                  />
                  {result.results.length}
                </label>
                <span className="small muted">
                  {selected.size} selected ({selectedOnPage} on this page)
                </span>
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
                  {addState === 'saving' ? 'Importing…' : `Add ${selected.size}`}
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
              {result.results.map((hit, index) => (
                <ForayHit
                  key={hit.login}
                  hit={hit}
                  selected={selected.has(hit.login)}
                  selectionDisabled={busy}
                  onToggle={(event) => toggle(hit, index, event)}
                />
              ))}
            </div>
          )}

          {result.page_count > 1 ? (
            <div className="row" style={{ justifyContent: 'center', gap: 8 }}>
              <button
                type="button"
                disabled={busy || page === 0}
                onClick={() => setPage((value) => Math.max(0, value - 1))}
              >
                Previous
              </button>
              {pageIndexes.length ? (
                pageIndexes.map((pageIndex) => (
                  <button
                    type="button"
                    className={pageIndex === page ? 'primary' : 'ghost'}
                    key={pageIndex}
                    disabled={busy || pageIndex === page}
                    onClick={() => setPage(pageIndex)}
                    aria-label={`Page ${pageIndex + 1}`}
                  >
                    {pageIndex + 1}
                  </button>
                ))
              ) : (
                <span className="small muted">
                  Page {result.page} of {result.page_count}
                </span>
              )}
              <button
                type="button"
                disabled={busy || !result.has_more}
                onClick={() => setPage((value) => value + 1)}
              >
                Next
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
