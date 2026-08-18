/**
 * Sequence editor: step list with a computed schedule preview, per-step
 * editing with variable chips and send-time controls, preview-and-test
 * against a real candidate, and the enrollment list.
 *
 * Steps save via PUT /steps (replace-all). The backend 409s while
 * enrollments are mid-flight; the editor offers "pause & save".
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';

// Chip catalog mirrors the variable bag in outreach/personalize.py base_context.
const VARIABLES = [
  ['first_name', '{{ first_name | there }}'],
  ['current_title', '{{ current_title }}'],
  ['current_company', '{{ current_company }}'],
  ['top_skill', '{{ top_skill }}'],
  ['role_title', '{{ role_title | the role }}'],
  ['opening_line', '{{ opening_line }}'],
  ['why_relevant', '{{ why_relevant }}'],
  ['subject_hint', '{{ subject_hint | A quick note }}'],
];

const TIMEZONES = [
  'America/Los_Angeles',
  'America/Denver',
  'America/Chicago',
  'America/New_York',
  'Europe/London',
  'Europe/Berlin',
  'Asia/Kolkata',
  'Asia/Singapore',
];

const EMPTY_STEP = {
  channel: 'email',
  delay_days: 3,
  delay_unit: 'business_days',
  send_at_time: '',
  send_timezone: '',
  subject_template: '',
  body_template: '',
};

function addDelay(date, days, unit) {
  const result = new Date(date);
  if (unit === 'business_days') {
    let remaining = days;
    while (remaining > 0) {
      result.setDate(result.getDate() + 1);
      if (result.getDay() !== 0 && result.getDay() !== 6) remaining -= 1;
    }
  } else {
    result.setDate(result.getDate() + days);
  }
  return result;
}

function schedulePreview(steps) {
  let cursor = new Date();
  return steps.map((step, index) => {
    cursor = addDelay(cursor, Number(step.delay_days) || 0, step.delay_unit);
    const date = cursor.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    if (index === 0 && (Number(step.delay_days) || 0) === 0) return 'on enrollment';
    const at = step.send_at_time ? ` · ${step.send_at_time}` : '';
    return `~${date}${at}`;
  });
}

function normalizeSteps(steps) {
  return steps.map((step) => ({
    channel: step.channel ?? 'email',
    delay_days: Number(step.delay_days) || 0,
    delay_unit: step.delay_unit ?? 'days',
    send_at_time: step.send_at_time ?? '',
    send_timezone: step.send_timezone ?? '',
    subject_template: step.subject_template ?? '',
    body_template: step.body_template ?? '',
  }));
}

export function SequenceEditorPage() {
  const { sequenceId } = useParams();
  const sequence = useAsync(() => api.getSequence(sequenceId), [sequenceId]);
  const enrollments = useAsync(() => api.listEnrollments(sequenceId), [sequenceId]);
  const candidates = useAsync(() => api.listCandidates({ limit: 25 }), []);
  const microsoft = useAsync(() => api.microsoftStatus(), []);

  const [meta, setMeta] = useState(null);
  const [steps, setSteps] = useState([]);
  const [selected, setSelected] = useState(0);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [needsPause, setNeedsPause] = useState(false);
  const [preview, setPreview] = useState(null);
  const [previewCandidate, setPreviewCandidate] = useState('');
  const bodyRef = useRef(null);
  const subjectRef = useRef(null);
  const lastFocused = useRef('body');

  useEffect(() => {
    if (!sequence.data) return;
    setMeta({
      name: sequence.data.name,
      personalize: sequence.data.personalize,
      stop_on_reply: sequence.data.stop_on_reply,
      from_email: sequence.data.from_email,
      from_name: sequence.data.from_name,
      mailbox_id: sequence.data.mailbox_id,
    });
    setSteps(normalizeSteps(sequence.data.steps));
    setSelected(0);
  }, [sequence.data]);

  const savedSteps = useMemo(
    () => (sequence.data ? normalizeSteps(sequence.data.steps) : []),
    [sequence.data],
  );
  const stepsDirty = JSON.stringify(steps) !== JSON.stringify(savedSteps);
  const metaDirty =
    meta && sequence.data
      ? ['name', 'personalize', 'stop_on_reply', 'from_email', 'from_name', 'mailbox_id'].some(
          (key) => meta[key] !== sequence.data[key],
        )
      : false;
  const dirty = stepsDirty || metaDirty;
  const schedule = useMemo(() => schedulePreview(steps), [steps]);

  if (sequence.loading || !meta) return <Loading label="Loading sequence…" />;
  if (sequence.error) return <ErrorBox error={sequence.error} />;

  const status = sequence.data.status;

  function updateStep(index, patch) {
    setSteps((previous) => previous.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }

  function insertVariable(snippet) {
    const isSubject = lastFocused.current === 'subject';
    const node = isSubject ? subjectRef.current : bodyRef.current;
    const key = isSubject ? 'subject_template' : 'body_template';
    const value = steps[selected][key];
    const at = node ? node.selectionStart ?? value.length : value.length;
    updateStep(selected, { [key]: value.slice(0, at) + snippet + value.slice(at) });
  }

  async function save({ pauseFirst = false } = {}) {
    setSaving(true);
    setError(null);
    setNotice('');
    setNeedsPause(false);
    try {
      if (pauseFirst) await api.setSequenceStatus(sequenceId, 'paused');
      if (metaDirty) await api.updateSequence(sequenceId, meta);
      if (stepsDirty) await api.replaceSteps(sequenceId, steps);
      await sequence.reload();
      setNotice('Saved');
    } catch (caught) {
      if (caught.status === 409) setNeedsPause(true);
      else setError(caught);
    } finally {
      setSaving(false);
    }
  }

  async function toggleStatus() {
    setError(null);
    try {
      await api.setSequenceStatus(sequenceId, status === 'active' ? 'paused' : 'active');
      await sequence.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  async function runPreview() {
    const candidateId = previewCandidate || candidates.data?.[0]?.id;
    if (!candidateId) {
      setError(new Error('No candidates to preview against'));
      return;
    }
    setError(null);
    try {
      setPreview(
        await api.previewSequence(sequenceId, {
          candidate_id: candidateId,
          step_order: selected,
        }),
      );
    } catch (caught) {
      setError(caught);
    }
  }

  const step = steps[selected] ?? steps[0];

  return (
    <div className="stack">
      <div className="spread">
        <div className="row">
          <Link to="/sequences" className="muted small">
            ← Sequences
          </Link>
          <input
            type="text"
            className="inline-title"
            value={meta.name}
            onChange={(event) => setMeta({ ...meta, name: event.target.value })}
          />
          <Badge tone={status === 'active' ? 'strong' : ''}>{status}</Badge>
        </div>
        <div className="row">
          <button type="button" className="tiny" onClick={toggleStatus}>
            {status === 'active' ? 'Pause' : 'Activate'}
          </button>
          <button type="button" className="primary" disabled={!dirty || saving} onClick={() => save()}>
            {saving ? 'Saving…' : dirty ? 'Save' : 'Saved'}
          </button>
        </div>
      </div>

      <ErrorBox error={error} />
      {needsPause ? (
        <div className="card small spread">
          <span>Enrollments are mid-flight — pause the sequence to edit its steps.</span>
          <button type="button" className="tiny" onClick={() => save({ pauseFirst: true })}>
            Pause &amp; save
          </button>
        </div>
      ) : null}
      {notice ? <div className="card small">{notice}</div> : null}

      <div className="editor-grid">
        <div className="stack" style={{ gap: 8 }}>
          {steps.map((s, index) => (
            <button
              key={index}
              type="button"
              className={`step-item${index === selected ? ' active' : ''}`}
              onClick={() => {
                setSelected(index);
                setPreview(null);
              }}
            >
              <div className="spread">
                <strong className="small">Step {index + 1} · Email</strong>
                {steps.length > 1 ? (
                  <span
                    role="button"
                    tabIndex={-1}
                    className="muted small"
                    onClick={(event) => {
                      event.stopPropagation();
                      setSteps((previous) => previous.filter((_, i) => i !== index));
                      setSelected((current) => Math.max(0, current > index ? current - 1 : Math.min(current, steps.length - 2)));
                    }}
                  >
                    ✕
                  </span>
                ) : null}
              </div>
              <div className="muted small">sends {schedule[index]}</div>
            </button>
          ))}
          <button
            type="button"
            className="ghost"
            onClick={() => {
              setSteps((previous) => [...previous, { ...EMPTY_STEP }]);
              setSelected(steps.length);
            }}
          >
            + Add step
          </button>
        </div>

        <div className="stack">
          {step ? (
            <div className="card stack">
              <div className="row wrap small muted">
                <span>Variables:</span>
                {VARIABLES.map(([label, snippet]) => (
                  <button
                    key={label}
                    type="button"
                    className="tiny chip"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => insertVariable(snippet)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <input
                ref={subjectRef}
                type="text"
                placeholder="Subject"
                value={step.subject_template}
                onFocus={() => {
                  lastFocused.current = 'subject';
                }}
                onChange={(event) => updateStep(selected, { subject_template: event.target.value })}
              />
              <textarea
                ref={bodyRef}
                rows={10}
                placeholder="Body — use variables for anything candidate-specific"
                value={step.body_template}
                onFocus={() => {
                  lastFocused.current = 'body';
                }}
                onChange={(event) => updateStep(selected, { body_template: event.target.value })}
              />
              <div className="row wrap small">
                <label className="muted">
                  Wait{' '}
                  <input
                    type="number"
                    min="0"
                    max="90"
                    style={{ width: 64, display: 'inline-block' }}
                    value={step.delay_days}
                    onChange={(event) => updateStep(selected, { delay_days: event.target.value })}
                  />
                </label>
                <select
                  style={{ width: 150 }}
                  value={step.delay_unit}
                  onChange={(event) => updateStep(selected, { delay_unit: event.target.value })}
                >
                  <option value="days">days</option>
                  <option value="business_days">business days</option>
                </select>
                <label className="muted">
                  Send @{' '}
                  <input
                    type="time"
                    style={{ width: 120, display: 'inline-block' }}
                    value={step.send_at_time}
                    onChange={(event) => updateStep(selected, { send_at_time: event.target.value })}
                  />
                </label>
                <select
                  style={{ width: 200 }}
                  value={step.send_timezone}
                  onChange={(event) => updateStep(selected, { send_timezone: event.target.value })}
                >
                  <option value="">Recipient default (UTC)</option>
                  {TIMEZONES.map((zone) => (
                    <option key={zone} value={zone}>
                      {zone}
                    </option>
                  ))}
                </select>
              </div>
              <div className="row wrap small muted" style={{ alignItems: 'center' }}>
                <span>Send from</span>
                <select
                  style={{ width: 260 }}
                  value={meta.mailbox_id ?? ''}
                  onChange={(event) =>
                    setMeta({ ...meta, mailbox_id: event.target.value || null })
                  }
                >
                  <option value="">Server default</option>
                  {(microsoft.data?.connections ?? [])
                    .filter((connection) => connection.status === 'active')
                    .map((connection) => (
                      <option key={connection.id} value={connection.id}>
                        {connection.address} (Outlook)
                      </option>
                    ))}
                </select>
                {!(microsoft.data?.connections ?? []).some((c) => c.status === 'active') ? (
                  <span>
                    <Link to="/settings">Connect Outlook</Link> to send as yourself
                  </span>
                ) : null}
              </div>
            </div>
          ) : null}

          <div className="card stack">
            <div className="spread">
              <strong className="small">Preview &amp; test — step {selected + 1}</strong>
              <div className="row">
                <select
                  style={{ width: 220 }}
                  value={previewCandidate}
                  onChange={(event) => setPreviewCandidate(event.target.value)}
                >
                  {(candidates.data ?? []).map((candidate) => (
                    <option key={candidate.id} value={candidate.id}>
                      {candidate.full_name}
                    </option>
                  ))}
                </select>
                <button type="button" className="tiny" disabled={dirty} onClick={runPreview}>
                  Preview
                </button>
              </div>
            </div>
            {dirty ? <div className="muted small">Save your changes to preview them.</div> : null}
            {preview ? (
              <>
                <div className="small muted">Subject</div>
                <div>{preview.subject}</div>
                <div className="small muted">Body</div>
                <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{preview.body}</pre>
                <Badge tone="accent">
                  personalised by {preview.personalization?.personalized_by ?? 'template'}
                </Badge>
              </>
            ) : null}
          </div>

          <div className="card stack">
            <div className="spread">
              <strong className="small">Enrollments</strong>
              <div className="row small muted">
                <label>
                  <input
                    type="checkbox"
                    checked={meta.personalize}
                    onChange={(event) => setMeta({ ...meta, personalize: event.target.checked })}
                  />{' '}
                  AI personalisation
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={meta.stop_on_reply}
                    onChange={(event) => setMeta({ ...meta, stop_on_reply: event.target.checked })}
                  />{' '}
                  Stop on reply
                </label>
              </div>
            </div>
            <ErrorBox error={enrollments.error} />
            {enrollments.loading ? (
              <Loading label="Loading enrollments…" />
            ) : (enrollments.data ?? []).length ? (
              <div className="stack" style={{ gap: 6 }}>
                {enrollments.data.map((enrollment) => (
                  <div key={enrollment.id} className="row small">
                    <Badge tone={enrollment.status === 'active' ? 'strong' : ''}>
                      {enrollment.status}
                    </Badge>
                    <span className="mono muted">step {enrollment.current_step + 1}</span>
                    <span className="muted">
                      {enrollment.next_send_at
                        ? `next: ${new Date(enrollment.next_send_at).toLocaleString()}`
                        : enrollment.stopped_reason || ''}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <Empty>Nobody enrolled yet — add candidates from Search or a project pipeline.</Empty>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
