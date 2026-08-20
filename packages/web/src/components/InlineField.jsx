/**
 * Click-to-edit text, for the fields on a person's record.
 *
 * Optimistic, with a revert on failure: the alternative is a spinner on every
 * cell, and these saves are one small PATCH each. Escape restores the value
 * you started with, so an accidental click is never a change.
 */

import { useEffect, useRef, useState } from 'react';

export function InlineField({
  value,
  onSave,
  label,
  placeholder = '—',
  multiline = false,
  testId,
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? '');
  const [pending, setPending] = useState(false);
  const input = useRef(null);

  // Someone else's save (or a reload) should win over a stale local copy.
  useEffect(() => {
    if (!editing) setDraft(value ?? '');
  }, [value, editing]);

  useEffect(() => {
    if (editing && input.current) {
      input.current.focus();
      input.current.select?.();
    }
  }, [editing]);

  async function commit() {
    const next = draft.trim();
    setEditing(false);
    if (next === (value ?? '').trim()) return;
    setPending(true);
    try {
      await onSave(next);
    } catch {
      // The parent surfaces the error; here we just stop showing a value that
      // was never stored.
      setDraft(value ?? '');
    } finally {
      setPending(false);
    }
  }

  function onKeyDown(event) {
    if (event.key === 'Escape') {
      setDraft(value ?? '');
      setEditing(false);
    } else if (event.key === 'Enter' && !multiline) {
      event.preventDefault();
      commit();
    }
  }

  const Tag = multiline ? 'textarea' : 'input';

  return (
    <div className="stack" style={{ gap: 2 }}>
      {label ? <span className="field-label">{label}</span> : null}
      {editing ? (
        <Tag
          ref={input}
          className="inline-field"
          value={draft}
          data-testid={testId}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={onKeyDown}
        />
      ) : (
        <button
          type="button"
          className={`inline-field readonly${pending ? ' pending' : ''}`}
          data-testid={testId}
          onClick={() => setEditing(true)}
          title="Click to edit"
        >
          {value || <span className="muted">{placeholder}</span>}
        </button>
      )}
    </div>
  );
}
