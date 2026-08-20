/**
 * Tags on a person.
 *
 * The API takes the whole set rather than add/remove deltas, which is exactly
 * what a chip editor wants: no ordering problems, and re-saving the same set
 * is a no-op.
 *
 * Suggestions come from the tags the org already uses, so the vocabulary
 * converges without anyone maintaining a list — and a tag nobody uses any more
 * stops being suggested on its own.
 */

import { useState } from 'react';

import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';
import { ErrorBox } from './common.jsx';

export function TagEditor({ candidateId, tags, onChanged }) {
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const known = useAsync(() => api.listTags(), []);

  const current = tags ?? [];

  async function save(next) {
    setBusy(true);
    setError(null);
    try {
      await api.updateCandidate(candidateId, { tags: next });
      setDraft('');
      onChanged();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  function add(value) {
    const tag = (value ?? draft).trim().toLowerCase();
    if (!tag || current.includes(tag)) return setDraft('');
    return save([...current, tag]);
  }

  const suggestions = (known.data ?? [])
    .map((row) => row.tag)
    .filter((tag) => !current.includes(tag))
    .slice(0, 8);

  return (
    <div className="stack" style={{ gap: 6 }} data-testid="tag-editor">
      <span className="field-label">Tags</span>
      <div className="row wrap" style={{ gap: 6 }}>
        {current.map((tag) => (
          <span key={tag} className="badge tag" data-testid="tag-chip">
            {tag}
            <button
              type="button"
              className="tag-remove"
              aria-label={`Remove ${tag}`}
              disabled={busy}
              onClick={() => save(current.filter((t) => t !== tag))}
            >
              ×
            </button>
          </span>
        ))}
        {current.length === 0 ? <span className="muted small">None yet</span> : null}
      </div>

      <div className="row" style={{ gap: 6 }}>
        <input
          value={draft}
          placeholder="Add a tag…"
          data-testid="tag-input"
          list="known-tags"
          disabled={busy}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              add();
            }
          }}
        />
        <datalist id="known-tags">
          {(known.data ?? []).map((row) => (
            <option key={row.tag} value={row.tag} />
          ))}
        </datalist>
        <button type="button" disabled={busy || !draft.trim()} onClick={() => add()}>
          Add
        </button>
      </div>

      {suggestions.length ? (
        <div className="row wrap" style={{ gap: 4 }}>
          <span className="muted small">Used before:</span>
          {suggestions.map((tag) => (
            <button
              key={tag}
              type="button"
              className="tiny ghost"
              disabled={busy}
              onClick={() => add(tag)}
            >
              {tag}
            </button>
          ))}
        </div>
      ) : null}

      <ErrorBox error={error} />
    </div>
  );
}
