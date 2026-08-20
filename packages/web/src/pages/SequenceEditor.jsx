/**
 * Sequence editor: step list with a computed schedule preview, per-step
 * editing with variable chips and send-time controls, preview-and-test
 * against a real candidate, and the enrollment list.
 *
 * Steps save via PUT /steps (replace-all). The backend 409s while
 * enrollments are mid-flight; the editor offers "pause & save".
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../components/common.jsx';
import { EmailBodyEditor, looksLikeHtml } from '../components/EmailBodyEditor.jsx';
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
  // Not a variable: an inline instruction the model resolves per candidate at
  // send time, with the earlier emails of the thread as context.
  ['ai: …', '{{ai: say why this role is a fit}}'],
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

/**
 * Mirrors schedule_step_at() in outreach/sequencer.py. A step with a send time
 * never sends "on enrollment" — it waits for that time — so saying otherwise
 * would contradict what the sequencer actually does.
 *
 * The snap is computed in the browser's timezone, so the date can be off by a
 * day for a step configured in a distant zone; the zone is labelled rather
 * than silently converted.
 */
function schedulePreview(steps) {
  const now = new Date();
  let cursor = now;
  return steps.map((step, index) => {
    cursor = addDelay(cursor, Number(step.delay_days) || 0, step.delay_unit);

    if (step.send_at_time) {
      const [hour, minute] = step.send_at_time.split(':').map(Number);
      const snapped = new Date(cursor);
      snapped.setHours(hour, minute, 0, 0);
      if (snapped < now) snapped.setDate(snapped.getDate() + 1);
      if (step.delay_unit === 'business_days') {
        while (snapped.getDay() === 0 || snapped.getDay() === 6) {
          snapped.setDate(snapped.getDate() + 1);
        }
      }
      cursor = snapped;
      const date = snapped.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
      const zone = step.send_timezone ? ` ${step.send_timezone.split('/').pop().replace(/_/g, ' ')}` : '';
      return `~${date} · ${step.send_at_time}${zone}`;
    }

    if (index === 0 && (Number(step.delay_days) || 0) === 0) return 'on enrollment';
    const date = cursor.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    return `~${date}`;
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

/**
 * Who to enrol. Defaults to the sequence's own project pipeline — the people
 * already shortlisted for this role — and falls back to a search over every
 * candidate for anyone outside it.
 */
function CandidatePicker({ projectId, enrolledIds, busy, firstSendLabel, onCancel, onEnroll }) {
  const [source, setSource] = useState(projectId ? 'project' : 'all');
  const [query, setQuery] = useState('');
  const [picked, setPicked] = useState(() => new Set());
  // 'scheduled' honours the first step's send time (an afternoon enrolment
  // against a 9:00 step waits until tomorrow morning); 'now' sends on enrol.
  // The label shows WHEN "scheduled" actually is, so the wait is never a
  // surprise.
  const [sendMode, setSendMode] = useState('scheduled');

  const pipeline = useAsync(
    () => (projectId ? api.getPipeline(projectId) : Promise.resolve([])),
    [projectId],
  );
  const all = useAsync(() => api.listCandidates({ q: query, limit: 50 }), [query]);

  const rows =
    source === 'project'
      ? (pipeline.data ?? []).map((entry) => entry.candidate)
      : (all.data ?? []);
  const available = rows.filter((candidate) => !enrolledIds.has(candidate.id));
  const chosen = available.filter((candidate) => picked.has(candidate.id));
  const withoutEmail = chosen.filter((candidate) => !(candidate.emails ?? []).length);

  function toggle(id) {
    setPicked((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="card stack" style={{ gap: 8 }} data-testid="candidate-picker">
      <div className="row small">
        {projectId ? (
          <select style={{ width: 200 }} value={source} onChange={(event) => setSource(event.target.value)}>
            <option value="project">This project&apos;s pipeline</option>
            <option value="all">All candidates</option>
          </select>
        ) : null}
        {source === 'all' ? (
          <input
            type="search"
            placeholder="Search candidates…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        ) : null}
        <button
          type="button"
          className="tiny ghost"
          onClick={() => setPicked(new Set(available.map((candidate) => candidate.id)))}
        >
          select all
        </button>
      </div>

      {(source === 'project' ? pipeline.loading : all.loading) ? (
        <Loading label="Loading candidates…" />
      ) : available.length ? (
        <div className="stack" style={{ gap: 4, maxHeight: 260, overflowY: 'auto' }}>
          {available.map((candidate) => (
            <label
              key={candidate.id}
              className="row small"
              style={{ gap: 6 }}
              data-testid="picker-row"
            >
              <input
                type="checkbox"
                checked={picked.has(candidate.id)}
                onChange={() => toggle(candidate.id)}
              />
              <span>{candidate.full_name}</span>
              <span className="muted">
                {candidate.current_title || '—'}
                {candidate.current_company ? ` · ${candidate.current_company}` : ''}
              </span>
              {!(candidate.emails ?? []).length ? <span className="muted">no email</span> : null}
            </label>
          ))}
        </div>
      ) : (
        <Empty>
          {source === 'project'
            ? 'Everyone in this pipeline is already enrolled.'
            : 'No candidates match.'}
        </Empty>
      )}

      <div className="row wrap small" data-testid="send-mode">
        <span className="muted">First email:</span>
        <label className="row" style={{ gap: 4 }}>
          <input
            type="radio"
            name="send-mode"
            checked={sendMode === 'scheduled'}
            onChange={() => setSendMode('scheduled')}
          />
          at the scheduled time{firstSendLabel ? ` (${firstSendLabel})` : ''}
        </label>
        <label className="row" style={{ gap: 4 }}>
          <input
            type="radio"
            name="send-mode"
            checked={sendMode === 'now'}
            onChange={() => setSendMode('now')}
          />
          right away
        </label>
      </div>

      <div className="row spread">
        <span className="small muted">
          {withoutEmail.length
            ? `${withoutEmail.length} of ${chosen.length} have no email yet — they wait until one is found.`
            : ''}
        </span>
        <div className="row">
          <button type="button" className="tiny ghost" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="tiny primary"
            disabled={!chosen.length || busy}
            onClick={() => onEnroll(chosen.map((candidate) => candidate.id), sendMode === 'now')}
          >
            {busy ? 'Enrolling…' : `Enrol ${chosen.length || ''}`.trim()}
          </button>
        </div>
      </div>
    </div>
  );
}

export function SequenceEditorPage() {
  const { sequenceId } = useParams();
  const navigate = useNavigate();
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
  const [picking, setPicking] = useState(false);
  const [enrolling, setEnrolling] = useState(false);
  const [enrollError, setEnrollError] = useState(null);
  const bodyRef = useRef(null);
  const subjectRef = useRef(null);
  const lastFocused = useRef('body');
  // Read inside the reload effect, which must not see a stale `dirty`.
  const dirtyRef = useRef(false);

  useEffect(() => {
    if (!sequence.data) return;
    // A reload that is not a save — pausing, activating, enrolling — must not
    // wipe edits in progress. Pausing is the natural response to the 409 that
    // step editing raises, so clobbering here loses exactly the work the user
    // was trying to save.
    if (dirtyRef.current && meta !== null) return;
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
  dirtyRef.current = dirty;
  const schedule = useMemo(() => schedulePreview(steps), [steps]);

  if (sequence.loading || !meta) return <Loading label="Loading sequence…" />;
  if (sequence.error) return <ErrorBox error={sequence.error} />;

  const status = sequence.data.status;

  function updateStep(index, patch) {
    setSteps((previous) => previous.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }

  function insertVariable(snippet) {
    if (lastFocused.current !== 'subject') {
      lastFocused.current = 'body';
      bodyRef.current?.insert(snippet);
      return;
    }
    const node = subjectRef.current;
    const value = steps[selected].subject_template;
    const at = node ? node.selectionStart ?? value.length : value.length;
    updateStep(selected, { subject_template: value.slice(0, at) + snippet + value.slice(at) });
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

  async function enrollCandidates(candidateIds, sendNow = false) {
    setEnrolling(true);
    setEnrollError(null);
    setNotice('');
    try {
      // One request: with send_now the backend sends to exactly the people
      // being enrolled before responding, so the returned rows already show
      // the post-send state (current_step advanced, next = the follow-up).
      // The old flow fired a second unbounded /run over the whole sequence,
      // which could outlive the proxy timeout and leave this button stuck on
      // "Enrolling…" while mail actually went out.
      const rows = await api.enroll(sequenceId, {
        candidate_ids: candidateIds,
        project_id: sequence.data.project_id ?? null,
        send_now: sendNow,
      });
      const sentNow = (rows ?? []).filter((row) => row.current_step > 0).length;
      await enrollments.reload();
      setPicking(false);
      if (status !== 'active') {
        setNotice(`Enrolled ${candidateIds.length}. Activate the sequence to start sending.`);
      } else if (sendNow) {
        setNotice(
          sentNow
            ? `Enrolled ${candidateIds.length} — ${sentNow} email${sentNow === 1 ? '' : 's'} sent.`
            : `Enrolled ${candidateIds.length} — sending within a minute.`,
        );
      } else {
        setNotice(
          `Enrolled ${candidateIds.length} — first email goes out at the step's scheduled time.`,
        );
      }
    } catch (caught) {
      setEnrollError(caught);
    } finally {
      setEnrolling(false);
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

  async function remove() {
    if (
      !window.confirm(
        `Delete “${meta.name}”? Enrolments and sent-message history for this sequence go with it.`,
      )
    ) {
      return;
    }
    setError(null);
    try {
      await api.deleteSequence(sequenceId);
      navigate('/sequences');
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
          <button type="button" className="tiny ghost" onClick={remove}>
            Delete
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
              <EmailBodyEditor
                key={selected}
                ref={bodyRef}
                value={step.body_template}
                onFocus={() => {
                  lastFocused.current = 'body';
                }}
                onChange={(html) => updateStep(selected, { body_template: html })}
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
                {preview.ai_status === 'unavailable' ? (
                  <div className="card small" data-testid="ai-unavailable-banner">
                    No AI model is configured, so the [AI: …] parts below show where
                    generated text will go. Sending waits until a model can write them.
                  </div>
                ) : null}
                <div className="small muted">Subject</div>
                <div>{preview.subject}</div>
                <div className="small muted">Body</div>
                {looksLikeHtml(preview.body) ? (
                  <div
                    className="email-preview"
                    data-testid="preview-body"
                    dangerouslySetInnerHTML={{ __html: preview.body }}
                  />
                ) : (
                  <pre className="email-preview" data-testid="preview-body">
                    {preview.body}
                  </pre>
                )}
                <div className="row">
                  <Badge tone="accent">
                    personalised by {preview.personalization?.personalized_by ?? 'template'}
                  </Badge>
                  {preview.ai_status === 'resolved' ? (
                    <Badge tone="strong">AI fragments resolved</Badge>
                  ) : null}
                </div>
              </>
            ) : null}
          </div>

          <div className="card stack">
            <div className="spread">
              <div className="row">
                <strong className="small">Enrollments</strong>
                <button
                  type="button"
                  className="tiny primary"
                  onClick={() => setPicking((open) => !open)}
                >
                  {picking ? 'Close' : '+ Add people'}
                </button>
              </div>
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
            {enrollError ? <ErrorBox error={enrollError} /> : null}

            {/* Enrolling schedules; only an active sequence actually sends. Say
                so here rather than silently activating on the user's behalf. */}
            {status !== 'active' && (enrollments.data ?? []).some((e) => e.status === 'active') ? (
              <div className="card small spread" style={{ margin: 0 }} data-testid="paused-banner">
                <span>
                  {(enrollments.data ?? []).filter((e) => e.status === 'active').length} enrolled and
                  waiting — nothing sends while the sequence is {status}.
                </span>
                <button type="button" className="tiny primary" onClick={toggleStatus}>
                  Activate
                </button>
              </div>
            ) : null}

            {picking ? (
              <CandidatePicker
                projectId={sequence.data.project_id}
                enrolledIds={new Set((enrollments.data ?? []).map((e) => e.candidate_id))}
                busy={enrolling}
                firstSendLabel={schedule[0] === 'on enrollment' ? '' : schedule[0]}
                onCancel={() => setPicking(false)}
                onEnroll={enrollCandidates}
              />
            ) : null}

            {enrollments.loading ? (
              <Loading label="Loading enrollments…" />
            ) : (enrollments.data ?? []).length ? (
              <div className="stack" style={{ gap: 6 }}>
                {enrollments.data.map((enrollment) => (
                  <div key={enrollment.id} className="row small spread" data-testid="enrollment-row">
                    <span className="row" style={{ gap: 6 }}>
                      <Badge tone={enrollment.status === 'active' ? 'strong' : ''}>
                        {enrollment.status}
                      </Badge>
                      <span>{enrollment.candidate?.full_name ?? enrollment.candidate_id}</span>
                      {!(enrollment.candidate?.emails ?? []).length ? (
                        <span className="muted" title="No email on file — this step can't send yet">
                          no email
                        </span>
                      ) : null}
                    </span>
                    <span className="row muted" style={{ gap: 6 }}>
                      {/* "✓2 sent · next: step 3 …" — without the sent count,
                          a follow-up's schedule reads like the FIRST email is
                          still waiting (a real support incident). */}
                      {enrollment.current_step > 0 ? (
                        <span className="mono">✓{enrollment.current_step} sent</span>
                      ) : null}
                      <span>
                        {enrollment.next_send_at
                          ? `next: step ${enrollment.current_step + 1} · ${new Date(enrollment.next_send_at).toLocaleString()}`
                          : enrollment.stopped_reason || (enrollment.status === 'completed' ? 'all steps sent' : '')}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <Empty>Nobody enrolled yet — use “Add people” to enrol candidates.</Empty>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
