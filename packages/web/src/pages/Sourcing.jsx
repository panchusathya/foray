import { useEffect, useRef, useState } from 'react';

import { Badge, Empty, ErrorBox, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { foray } from '../lib/foray.js';
import { forayHitToCandidate, partitionByEmail } from '../lib/forayImport.js';
import {
  countOnPage,
  pageWindow,
  selectAllFrom,
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

function ForayHit({ hit, selected, onToggle, selectionDisabled = false }) {
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

  const highlights = hit.evidence?.highlights ?? hit.highlights ?? [];
  const highlightSource = hit.highlight_source ?? hit.evidence?.highlight_source;

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
              {hit.github_url ? (
                <a href={hit.github_url} target="_blank" rel="noreferrer noopener">
                  {hit.name || hit.login}
                </a>
              ) : (
                <span>{hit.name || hit.login}</span>
              )}
              {hit.source === 'linkedin' ? (
                <span className="badge" style={{ marginLeft: 8 }} data-testid="source-badge">
                  LinkedIn
                </span>
              ) : null}
            </h3>
            <div className="meta">
              {meta}
              {hit.inferred?.employer ? <span className="badge" style={{ marginLeft: 6 }}>inferred</span> : null}
              {highlightSource ? (
                <span className="badge" style={{ marginLeft: 6 }} data-testid="highlight-source">
                  {highlightSource}
                </span>
              ) : null}
            </div>
          </div>
          <ContactChip hit={hit} />
        </div>

        {badge ? (
          <p className="small" style={{ color: 'var(--accent-light)', marginBottom: 0 }}>
            {badge}
          </p>
        ) : null}

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
          {hit.github_url ? (
            <a className="badge" href={hit.github_url} target="_blank" rel="noreferrer noopener">
              GitHub
            </a>
          ) : null}
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
  const [activeQuery, setActiveQuery] = useState(null);
  const [activeContactable, setActiveContactable] = useState(false);
  const [page, setPage] = useState(0);
  const [nonce, setNonce] = useState(0);
  const [parserLabel, setParserLabel] = useState(null);
  const [selectingAll, setSelectingAll] = useState(false);

  const [selected, setSelected] = useState(new Map()); // login -> hit
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState('');
  const [addState, setAddState] = useState('');
  const [showSkeleton, setShowSkeleton] = useState(false);
  const lastIndex = useRef(null);
  const searchRequest = useRef(0);
  const selectAllRequest = useRef(0);
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
    const started = performance.now();
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
        setElapsed(Math.round(performance.now() - started));
        if (page === 0) setParserLabel(response.parser);
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
    selectAllRequest.current += 1;
    setError(null);
    setAddState('');
    setSelectingAll(false);
    setActiveQuery(value);
    setActiveContactable(contactableOnly);
    setPage(0);
    setNonce((value) => value + 1);
    setSelected(new Map());
    setParserLabel(null);
    lastIndex.current = null;
  }

  async function selectAllMatching() {
    if (!activeQuery || !result?.paginable) return;
    const request = ++selectAllRequest.current;
    setSelectingAll(true);
    setError(null);
    try {
      const response = await foray.search(activeQuery, {
        limit: result.paginable,
        offset: 0,
        hasContact: activeContactable,
      });
      if (request === selectAllRequest.current) {
        setSelected(selectAllFrom(response.results));
      }
    } catch (caught) {
      if (request === selectAllRequest.current) setError(caught);
    } finally {
      if (request === selectAllRequest.current) setSelectingAll(false);
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

  const pageHits = result?.results ?? [];
  const selectedOnPage = countOnPage(selected, pageHits);
  const pageIndexes = result?.page_count <= 12
    ? pageWindow(page, result?.page_count ?? 0, 12)
    : [];

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

      {showSkeleton ? (
        <div className="card small muted" data-testid="search-skeleton">
          Searching…
        </div>
      ) : null}

      {result ? (
        <div className="stack" style={{ opacity: busy ? 0.55 : 1 }} aria-busy={busy}>
          <div className="card spread">
            <span className="small muted">
              {result.archetype ? result.archetype.label : 'semantic search'} · parser:{' '}
              {parserLabel ?? result.parser} · {result.returned} shown · page {result.page} of{' '}
              {result.page_count} · {result.total_matched} matched
              {result.min_evidence != null ? ` · min evidence ${result.min_evidence}` : ''}
              {` · reranked ${result.reranked ? result.rerank_depth : 'off'}`}
              {elapsed != null ? ` · ${elapsed}ms` : ''}
            </span>
            <span className="small muted">display floor {result.display_floor}</span>
          </div>

          {result.message ? <div className="card small">{result.message}</div> : null}

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
                    disabled={busy || selectingAll}
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
                  Select all {result.results.length} on this page
                </label>
                {result.paginable > result.results.length ? (
                  <button
                    type="button"
                    className="tiny ghost"
                    onClick={selectAllMatching}
                    disabled={busy || selectingAll}
                  >
                    {selectingAll
                      ? 'Selecting…'
                      : `Select all ${result.paginable} matching (${Math.min(result.rerank_depth || 0, result.paginable)} reranked)`}
                  </button>
                ) : null}
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
              {result.results.map((hit, index) => {
                const depth = result.rerank_depth || 0;
                const divider =
                  result.reranked &&
                  depth > 0 &&
                  hit.rank === depth + 1;
                return (
                  <div key={hit.login}>
                    {divider ? (
                      <div className="small muted" style={{ margin: '8px 0' }}>
                        Reranked top {depth}. Rows below keep retrieval order.
                      </div>
                    ) : null}
                    <ForayHit
                      hit={hit}
                      selected={selected.has(hit.login)}
                      selectionDisabled={busy || selectingAll}
                      onToggle={(event) => toggle(hit, index, event)}
                    />
                  </div>
                );
              })}
            </div>
          )}

          {result.truncated ? (
            <div className="small muted">
              {result.total_matched} matched; {result.paginable} ranked candidates are reachable
              from the bounded retrieval pool. Narrow the query to reach the rest.
            </div>
          ) : null}

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
