/**
 * Freeform notes on a person.
 *
 * Bodies render as plain text in a <pre>, for the same reason ContactHistory
 * does: there is no sanitizer in this repo, and notes are user-authored. No
 * markdown — a half-rendered note is worse than a plain one.
 */

import { useEffect, useState } from 'react';

import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';
import { Empty, ErrorBox, Loading } from './common.jsx';

function when(value) {
  if (!value) return '';
  const then = new Date(value);
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 30) return `${days}d ago`;
  return then.toLocaleDateString();
}

function Note({ note, onChanged }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(note.body);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    const body = draft.trim();
    if (!body || body === note.body) return setEditing(false);
    setBusy(true);
    setError(null);
    try {
      await api.updateNote(note.id, { body });
      setEditing(false);
      onChanged();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm('Delete this note?')) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteNote(note.id);
      onChanged();
    } catch (caught) {
      setError(caught);
      setBusy(false);
    }
  }

  return (
    <div className="note" data-testid="note">
      <div className="row spread">
        <span className="byline">
          {note.author_name || 'Someone'} · {when(note.created_at)}
          {note.edited_at ? ' · edited' : ''}
        </span>
        <span className="row" style={{ gap: 6 }}>
          <button
            type="button"
            className="tiny ghost"
            disabled={busy}
            onClick={() => setEditing((was) => !was)}
            data-testid="note-edit"
          >
            {editing ? 'Cancel' : 'Edit'}
          </button>
          <button
            type="button"
            className="tiny ghost"
            disabled={busy}
            onClick={remove}
            data-testid="note-delete"
          >
            Delete
          </button>
        </span>
      </div>
      <ErrorBox error={error} />
      {editing ? (
        <div className="stack" style={{ gap: 6 }}>
          <textarea value={draft} onChange={(event) => setDraft(event.target.value)} />
          <div className="row">
            <button type="button" disabled={busy} onClick={save}>
              Save
            </button>
          </div>
        </div>
      ) : (
        <pre>{note.body}</pre>
      )}
    </div>
  );
}

export function NotesPanel({ candidateId, onCount }) {
  const notes = useAsync(() => api.listNotes(candidateId), [candidateId]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function add() {
    const body = draft.trim();
    if (!body) return;
    setBusy(true);
    setError(null);
    try {
      await api.createNote(candidateId, { body });
      setDraft('');
      notes.reload();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  const rows = notes.data ?? [];

  useEffect(() => {
    if (notes.data) onCount?.(rows.length);
  }, [notes.data, onCount, rows.length]);

  return (
    <div className="stack" data-testid="notes-panel">
      <div className="stack" style={{ gap: 6 }}>
        <textarea
          value={draft}
          placeholder="Add a note…"
          data-testid="note-composer"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            // Matches the send shortcut elsewhere in the app.
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) add();
          }}
        />
        <div className="row">
          <button type="button" disabled={busy || !draft.trim()} onClick={add}>
            {busy ? 'Saving…' : 'Add note'}
          </button>
        </div>
      </div>

      <ErrorBox error={error} />
      <ErrorBox error={notes.error} />

      {notes.loading ? (
        <Loading label="Loading notes…" />
      ) : rows.length ? (
        <div className="card stack" style={{ gap: 0 }}>
          {rows.map((note) => (
            <Note key={note.id} note={note} onChanged={notes.reload} />
          ))}
        </div>
      ) : (
        <Empty>No notes yet.</Empty>
      )}
    </div>
  );
}
