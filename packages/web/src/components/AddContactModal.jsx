/**
 * Add one person by hand.
 *
 * A duplicate address comes back as a 409 carrying the existing record, and is
 * rendered as a way through rather than as an error: the usual reason you hit
 * it is that the person is already in Contacts and you want to open them.
 *
 * Validation stays thin. EmailStr on the API is the real validator, and
 * request() already flattens FastAPI's 422 arrays into a readable message.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../lib/api.js';
import { ErrorBox } from './common.jsx';

const BLANK = {
  full_name: '',
  emails: '',
  current_title: '',
  current_company: '',
  linkedin_url: '',
  note: '',
};

export function AddContactModal({ onClose, onCreated }) {
  const [form, setForm] = useState(BLANK);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [duplicate, setDuplicate] = useState(null);
  const [added, setAdded] = useState('');

  function set(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
    setDuplicate(null);
  }

  async function submit(andAnother) {
    const name = form.full_name.trim();
    if (!name) return;

    setBusy(true);
    setError(null);
    setDuplicate(null);
    try {
      const payload = {
        full_name: name,
        source: 'manual',
        current_title: form.current_title.trim(),
        current_company: form.current_company.trim(),
        linkedin_url: form.linkedin_url.trim(),
      };
      const email = form.emails.trim();
      if (email) payload.emails = [email];

      const created = await api.createCandidate(payload);

      const note = form.note.trim();
      if (note) {
        // A failed note must not undo a successful create, so it is reported
        // rather than thrown: the person exists either way.
        try {
          await api.createNote(created.id, { body: note });
        } catch (caught) {
          setError(caught);
        }
      }

      if (andAnother) {
        setForm(BLANK);
        setAdded(created.full_name);
      } else {
        onCreated(created);
      }
    } catch (caught) {
      // ApiError renders a dict detail as "Request failed (409)", so the
      // useful part is on .detail rather than in the message.
      if (caught?.status === 409 && caught?.detail?.candidate_id) {
        setDuplicate(caught.detail);
      } else {
        setError(caught);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal card stack"
        data-testid="add-contact-modal"
        onClick={(event) => event.stopPropagation()}
      >
        <strong>Add contact</strong>

        {added ? (
          <span className="small muted" data-testid="add-another-note">
            Added {added}.
          </span>
        ) : null}

        <label className="stack" style={{ gap: 4 }}>
          <span className="field-label">Name</span>
          <input
            value={form.full_name}
            data-testid="add-name"
            onChange={(event) => set('full_name', event.target.value)}
          />
        </label>

        <label className="stack" style={{ gap: 4 }}>
          <span className="field-label">Email</span>
          <input
            value={form.emails}
            data-testid="add-email"
            onChange={(event) => set('emails', event.target.value)}
          />
        </label>

        <div className="grid two" style={{ gap: 10 }}>
          <label className="stack" style={{ gap: 4 }}>
            <span className="field-label">Title</span>
            <input
              value={form.current_title}
              data-testid="add-title"
              onChange={(event) => set('current_title', event.target.value)}
            />
          </label>
          <label className="stack" style={{ gap: 4 }}>
            <span className="field-label">Company</span>
            <input
              value={form.current_company}
              data-testid="add-company"
              onChange={(event) => set('current_company', event.target.value)}
            />
          </label>
        </div>

        <label className="stack" style={{ gap: 4 }}>
          <span className="field-label">LinkedIn</span>
          <input
            value={form.linkedin_url}
            data-testid="add-linkedin"
            onChange={(event) => set('linkedin_url', event.target.value)}
          />
        </label>

        <label className="stack" style={{ gap: 4 }}>
          <span className="field-label">First note (optional)</span>
          <textarea
            value={form.note}
            data-testid="add-note"
            onChange={(event) => set('note', event.target.value)}
          />
        </label>

        {duplicate ? (
          <div className="card small" data-testid="add-duplicate">
            <strong>{duplicate.full_name}</strong> already has {duplicate.email} —{' '}
            <Link to={`/contacts/${duplicate.candidate_id}`} onClick={onClose}>
              open their record →
            </Link>
          </div>
        ) : null}

        <ErrorBox error={error} />

        <div className="row spread">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <span className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className="ghost"
              disabled={busy || !form.full_name.trim()}
              onClick={() => submit(true)}
              data-testid="add-save-another"
            >
              Save and add another
            </button>
            <button
              type="button"
              disabled={busy || !form.full_name.trim()}
              onClick={() => submit(false)}
              data-testid="add-save"
            >
              {busy ? 'Adding…' : 'Add contact'}
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
