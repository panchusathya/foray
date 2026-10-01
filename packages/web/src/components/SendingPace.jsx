// Settings: how fast and when sequence email goes out.
//
// Two org settings, both read and written through /v1/org/sending:
// * the gap between two emails from the same sending domain (every mailbox
//   @goforay.io shares one clock -- providers judge the domain);
// * sending hours: candidate email only inside a daily window, in a chosen
//   timezone (Pacific by default), on chosen weekdays.
// Edits are local until Save, so half-typed times never reach the sender.
import { useEffect, useState } from 'react';

import { api } from '../lib/api.js';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const ZONES = [
  'America/Los_Angeles',
  'America/Denver',
  'America/Chicago',
  'America/New_York',
  'Europe/London',
  'Europe/Berlin',
  'Asia/Kolkata',
  'Asia/Singapore',
  'Australia/Sydney',
  'UTC',
];

function draftFrom(sending) {
  return {
    send_gap_enabled: sending?.send_gap_enabled ?? true,
    send_gap_seconds: String(sending?.send_gap_seconds ?? 40),
    send_window_enabled: sending?.send_window_enabled ?? false,
    send_window_start: sending?.send_window_start ?? '09:00',
    send_window_end: sending?.send_window_end ?? '17:00',
    send_window_tz: sending?.send_window_tz ?? 'America/Los_Angeles',
    send_window_days: sending?.send_window_days ?? [0, 1, 2, 3, 4],
    send_window_breaks: (sending?.send_window_breaks ?? []).map((b) => ({ ...b })),
  };
}

function when(value) {
  if (!value) return '';
  return new Date(value).toLocaleString(undefined, {
    weekday: 'short',
    hour: 'numeric',
    minute: '2-digit',
  });
}

export function SendingPace({ sending, onSaved }) {
  const [draft, setDraft] = useState(() => draftFrom(sending));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [note, setNote] = useState('');

  useEffect(() => setDraft(draftFrom(sending)), [sending]);

  const saved = draftFrom(sending);
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const seconds = Number.parseInt(draft.send_gap_seconds, 10);
  const perHour = Number.isFinite(seconds) && seconds > 0 ? Math.floor(3600 / seconds) : null;

  function set(patch) {
    setDraft((current) => ({ ...current, ...patch }));
    setNote('');
  }

  function setBreak(index, patch) {
    set({
      send_window_breaks: draft.send_window_breaks.map((b, i) => (i === index ? { ...b, ...patch } : b)),
    });
  }

  function addBreak() {
    set({ send_window_breaks: [...draft.send_window_breaks, { start: '12:00', end: '13:00' }] });
  }

  function removeBreak(index) {
    set({ send_window_breaks: draft.send_window_breaks.filter((_, i) => i !== index) });
  }

  function toggleDay(day) {
    const next = new Set(draft.send_window_days);
    if (next.has(day)) next.delete(day);
    else next.add(day);
    set({ send_window_days: [...next].sort((a, b) => a - b) });
  }

  async function save() {
    setError(null);
    if (draft.send_gap_enabled && !(seconds >= 1 && seconds <= 3600)) {
      setError(new Error('The gap must be between 1 and 3600 seconds.'));
      return;
    }
    if (draft.send_window_enabled && !draft.send_window_days.length) {
      setError(new Error('Pick at least one sending day.'));
      return;
    }
    if (draft.send_window_breaks.some((b) => !b.start || !b.end || b.start >= b.end)) {
      setError(new Error('Each break needs an end time after its start.'));
      return;
    }
    if (draft.send_window_start === draft.send_window_end) {
      setError(new Error('Sending hours need different start and end times.'));
      return;
    }
    setBusy(true);
    try {
      await api.patchOrgSending({
        ...draft,
        send_gap_seconds: draft.send_gap_enabled ? seconds : undefined,
      });
      await onSaved();
      setNote('Saved.');
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  const windowState = sending?.send_window_enabled
    ? sending.send_window_open_now
      ? `Open now · closes ${when(sending.send_window_next_change)}`
      : `Closed now · opens ${when(sending.send_window_next_change)}`
    : 'Off — email can go out at any hour';

  return (
    <div className="card stack" data-testid="sending-pace">
      <div>
        <strong>Sending pace &amp; hours</strong>
        <div className="small muted">
          Applies to every sequence. Mailboxes on the same domain share one clock, because mail
          providers judge a domain, not a mailbox.
        </div>
      </div>

      <div className="pace-row">
        <label className="switch-label">
          <input
            type="checkbox"
            checked={draft.send_gap_enabled}
            onChange={(event) => set({ send_gap_enabled: event.target.checked })}
            data-testid="gap-toggle"
          />
          Space out sends on each domain
        </label>
        <span className="row" style={{ gap: 6 }}>
          one email every
          <input
            type="number"
            min="1"
            max="3600"
            value={draft.send_gap_seconds}
            disabled={!draft.send_gap_enabled}
            onChange={(event) => set({ send_gap_seconds: event.target.value })}
            style={{ width: 80 }}
            aria-label="Seconds between emails on one domain"
            data-testid="gap-seconds"
          />
          seconds
        </span>
        <span className="muted small">
          {draft.send_gap_enabled
            ? perHour
              ? `≈ ${perHour} per hour per domain`
              : ''
            : 'Off: emails go as fast as they are due — not recommended'}
        </span>
      </div>

      <div className="pace-row">
        <label className="switch-label">
          <input
            type="checkbox"
            checked={draft.send_window_enabled}
            onChange={(event) => set({ send_window_enabled: event.target.checked })}
            data-testid="window-toggle"
          />
          Only send during working hours
        </label>
        <span className="row wrap" style={{ gap: 6 }}>
          from
          <input
            type="time"
            value={draft.send_window_start}
            disabled={!draft.send_window_enabled}
            onChange={(event) => set({ send_window_start: event.target.value })}
            aria-label="Sending hours start"
            data-testid="window-start"
          />
          to
          <input
            type="time"
            value={draft.send_window_end}
            disabled={!draft.send_window_enabled}
            onChange={(event) => set({ send_window_end: event.target.value })}
            aria-label="Sending hours end"
            data-testid="window-end"
          />
          <select
            value={draft.send_window_tz}
            disabled={!draft.send_window_enabled}
            onChange={(event) => set({ send_window_tz: event.target.value })}
            aria-label="Sending hours timezone"
            data-testid="window-tz"
          >
            {[...new Set([draft.send_window_tz, ...ZONES])].map((zone) => (
              <option key={zone} value={zone}>
                {zone === 'America/Los_Angeles' ? 'Pacific (America/Los_Angeles)' : zone}
              </option>
            ))}
          </select>
        </span>
      </div>
      <div className="row wrap" style={{ gap: 6 }} data-testid="window-days">
        {DAYS.map((label, day) => (
          <button
            key={label}
            type="button"
            className={`day-chip${draft.send_window_days.includes(day) ? ' on' : ''}`}
            aria-pressed={draft.send_window_days.includes(day)}
            disabled={!draft.send_window_enabled}
            onClick={() => toggleDay(day)}
          >
            {label}
          </button>
        ))}
        <span className="muted small" data-testid="window-state">
          {windowState}
        </span>
      </div>
      <div className="stack" style={{ gap: 6 }} data-testid="window-breaks">
        {draft.send_window_breaks.map((b, index) => (
          <div className="row wrap" style={{ gap: 6 }} key={index} data-testid="window-break">
            <span className="small">Break from</span>
            <input
              type="time"
              value={b.start}
              disabled={!draft.send_window_enabled}
              onChange={(event) => setBreak(index, { start: event.target.value })}
              aria-label={`Break ${index + 1} start`}
            />
            <span className="small">to</span>
            <input
              type="time"
              value={b.end}
              disabled={!draft.send_window_enabled}
              onChange={(event) => setBreak(index, { end: event.target.value })}
              aria-label={`Break ${index + 1} end`}
            />
            <button
              type="button"
              className="tiny ghost"
              disabled={!draft.send_window_enabled}
              onClick={() => removeBreak(index)}
              aria-label={`Remove break ${index + 1}`}
            >
              Remove
            </button>
          </div>
        ))}
        <div>
          <button
            type="button"
            className="tiny ghost"
            disabled={!draft.send_window_enabled || draft.send_window_breaks.length >= 8}
            onClick={addBreak}
            data-testid="add-break"
          >
            + Add a break
          </button>
          <span className="muted small"> e.g. no sends over lunch, 12:00–1:00</span>
        </div>
      </div>
      <div className="small muted">
        Outside these hours and during breaks, emails wait in the Outbox (“Held · outside
        sending hours”) and keep their place in line. Follow-up reminders and draft-ready notes to yourself are not
        affected.
      </div>

      <div className="row" style={{ gap: 8 }}>
        <button
          type="button"
          className="tiny primary"
          disabled={!dirty || busy}
          onClick={save}
          data-testid="pace-save"
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
        {dirty ? (
          <button type="button" className="tiny ghost" onClick={() => setDraft(saved)}>
            Undo changes
          </button>
        ) : null}
        {note ? <span className="small muted">{note}</span> : null}
        {error ? <span className="small" style={{ color: 'var(--miss)' }}>{error.message}</span> : null}
      </div>
    </div>
  );
}
