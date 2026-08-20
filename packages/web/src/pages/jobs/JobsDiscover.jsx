import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox } from '../../components/common.jsx';
import { jobsApi } from '../../lib/jobs.js';
import { countOnPage, toggleOne, togglePage } from '../../lib/selection.js';

const bySourceId = (row) => row.source_id;

function messagesFromTurns(turns) {
  return turns.map((turn) => ({ role: turn.role, content: turn.content }));
}

export function JobsDiscover({ turns, setTurns, selected, setSelected }) {
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [addedNote, setAddedNote] = useState('');

  async function send() {
    const content = draft.trim();
    if (!content || busy) return;
    setBusy(true);
    setError(null);
    setAddedNote('');
    const nextTurns = [...turns, { role: 'user', content }];
    setTurns(nextTurns);
    setDraft('');
    try {
      const payload = await jobsApi.discoverTurn({
        messages: messagesFromTurns(nextTurns),
      });
      setTurns([
        ...nextTurns,
        {
          role: 'agent',
          content: payload.reply || '',
          questions: payload.questions || [],
          steps: payload.steps || [],
          results: payload.results || [],
          assumptions: payload.assumptions || [],
        },
      ]);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  async function addSelected() {
    const postings = [...selected.values()]
      .filter((row) => row?.title)
      .map((row) => ({
        title: row.title,
        company: row.company || '',
        location: row.location || '',
        jd_text: row.jd_text || '',
        source_url: row.source_url || '',
      }));
    if (!postings.length) return;
    setBusy(true);
    setError(null);
    try {
      await jobsApi.import({ source: 'exa', postings });
      setSelected(new Map());
      setAddedNote(`${postings.length} added to Overview.`);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  function onComposerKey(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  }

  return (
    <div className="stack">
      <ErrorBox error={error} />
      {addedNote ? (
        <div className="card small">
          {addedNote}{' '}
          <Link to="/jobs?view=overview" className="jobs-inline-link">
            View it in Overview →
          </Link>
        </div>
      ) : null}

      <div className="discover-thread">
        {!turns.length ? (
          <Empty>Describe the roles you want. We ask follow-ups, then search the open market.</Empty>
        ) : null}
        {turns.map((turn, index) => (
          <div
            key={`${turn.role}-${index}`}
            className={`discover-turn ${turn.role}`}
            data-testid="discover-turn"
          >
            {turn.role === 'user' ? (
              <div>{turn.content}</div>
            ) : (
              <>
                {turn.steps?.length ? (
                  <ul className="discover-steps" data-testid="discover-steps">
                    {turn.steps.map((step) => (
                      <li key={`${step.label}-${step.detail}`}>
                        ✓ {step.label}
                        {step.detail ? ` · ${step.detail}` : ''}
                        {step.count != null ? ` · ${step.count}` : ''}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {turn.questions?.length ? (
                  <ul className="discover-questions">
                    {turn.questions.map((question) => (
                      <li key={question}>{question}</li>
                    ))}
                  </ul>
                ) : null}
                {turn.content ? <div>{turn.content}</div> : null}
                {turn.results?.length ? (
                  <DiscoverResults
                    results={turn.results}
                    selected={selected}
                    setSelected={setSelected}
                  />
                ) : null}
              </>
            )}
          </div>
        ))}
      </div>

      {selected.size ? (
        <div className="card row spread wrap" data-testid="discover-add" style={{ gap: 10 }}>
          <span className="badge accent">{selected.size} selected</span>
          <span className="row" style={{ gap: 8 }}>
            <button type="button" disabled={busy} onClick={addSelected}>
              {busy ? 'Adding…' : 'Add to Jobs'}
            </button>
            <button type="button" className="tiny ghost" onClick={() => setSelected(new Map())}>
              Clear
            </button>
          </span>
        </div>
      ) : null}

      <form
        className="discover-composer card stack"
        data-testid="discover-composer"
        onSubmit={(event) => {
          event.preventDefault();
          send();
        }}
      >
        <textarea
          rows={3}
          placeholder="find me AI infra jobs"
          value={draft}
          disabled={busy}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onComposerKey}
        />
        <div className="row">
          <button type="submit" className="primary" data-testid="discover-send" disabled={busy || !draft.trim()}>
            {busy ? 'Working…' : 'Send'}
          </button>
        </div>
      </form>
    </div>
  );
}

function DiscoverResults({ results, selected, setSelected }) {
  const pageSelectedCount = countOnPage(selected, results, bySourceId);
  return (
    <div className="card dgrid-wrap">
      <table className="dgrid" data-testid="discover-results">
        <thead>
          <tr>
            <th className="pick">
              <input
                type="checkbox"
                aria-label="Select page"
                checked={results.length > 0 && pageSelectedCount === results.length}
                onChange={() => setSelected(togglePage(selected, results, bySourceId))}
              />
            </th>
            <th>Company</th>
            <th>Title</th>
            <th>Location</th>
            <th>Source URL</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {results.map((row) => {
            const on = selected.has(row.source_id);
            return (
              <tr
                key={row.source_id}
                className={on ? 'selected' : ''}
                data-testid="discover-row"
              >
                <td className="pick">
                  <input
                    type="checkbox"
                    checked={on}
                    aria-label={`Select ${row.title}`}
                    onChange={() => setSelected(toggleOne(selected, row, bySourceId))}
                  />
                </td>
                <td>{row.company || '—'}</td>
                <td>{row.title}</td>
                <td>{row.location || '—'}</td>
                <td>
                  {row.source_url ? (
                    <a href={row.source_url} target="_blank" rel="noreferrer">
                      {row.source_url}
                    </a>
                  ) : (
                    '—'
                  )}
                </td>
                <td>
                  {row.already_have && row.posting_id ? (
                    <Link to={`/jobs/${row.posting_id}`}>
                      <Badge tone="accent">already yours</Badge>
                    </Link>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
