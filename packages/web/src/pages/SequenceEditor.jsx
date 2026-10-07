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
import { EnrollmentList } from '../components/EnrollmentList.jsx';
import { ReenrolPanel } from '../components/ReenrolPanel.jsx';
import { SpamBadge, SpamPanel, useSpamCheck } from '../components/SpamScore.jsx';
import { api, providerLabel } from '../lib/api.js';
import { useCurrentProject } from '../lib/project.jsx';
import { malformedInStep } from '../lib/templates.js';
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
  // Resolves to whichever mailbox actually sends this step, so one sequence
  // rotating over a pool still signs each email correctly. First name is the
  // sign-off; sender_name (full) is there for the rare formal case.
  ['sender_first_name', '{{ sender_first_name }}'],
  // Two clear business days after the FIRST email in the thread, so every
  // follow-up proposes the same day rather than sliding forward.
  ['suggested_day', '{{ suggested_day }}'],
  ['suggested_date', '{{ suggested_date }}'],
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
const STOP_REASON_LABELS = {
  not_interested: 'not interested',
  unsubscribe: 'opted out',
  bounced: 'bounced',
  suppressed: 'suppressed',
  missing_email: 'no email',
  reach_score_below_floor: 'reach score too low',
  candidate_deleted: 'candidate deleted',
  replied_elsewhere: 'replied in another sequence',
  // The one stop a recruiter can undo: see canReenrol and the row's button.
  cancelled_by_user: 'cancelled by you',
};

// Only a stop the recruiter made themselves comes back. Every other stopped
// reason is the system's verdict on the person (replied, bounced, suppressed,
// no address) and re-enrolling would not change what stopped them.
function canReenrol(enrollment) {
  return enrollment.status === 'stopped' && enrollment.stopped_reason === 'cancelled_by_user';
}

// The right-hand text on an enrolment row: what happens next, or why nothing
// will. A reply is the interesting case -- "not now · follow up after 10 Sep"
// tells the recruiter the date the candidate asked for, and whether the
// reminder for it has gone out yet.
function enrollmentTrailer(enrollment) {
  const day = (value) => new Date(value).toLocaleDateString();
  if (enrollment.snoozed_until && new Date(enrollment.snoozed_until) > new Date()) {
    return `out of office · resumes step ${enrollment.current_step + 1} · ${new Date(enrollment.snoozed_until).toLocaleString()}`;
  }
  if (enrollment.next_send_at) {
    return `next: step ${enrollment.current_step + 1} · ${new Date(enrollment.next_send_at).toLocaleString()}`;
  }
  if (enrollment.stopped_reason === 'not_now') {
    if (!enrollment.follow_up_after) return 'not now';
    // A plain date, not a timestamp: parse it as local so it doesn't shift a day.
    const after = day(`${enrollment.follow_up_after}T00:00:00`);
    const reminder = enrollment.follow_up_reminder_sent_at ? 'reminded' : 'reminder scheduled';
    return `not now · follow up after ${after} · ${reminder}`;
  }
  if (enrollment.stopped_reason) {
    return STOP_REASON_LABELS[enrollment.stopped_reason] ?? enrollment.stopped_reason;
  }
  return enrollment.status === 'completed' ? 'all steps sent' : '';
}

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
          {/* Not "they wait until one is found", which is what this said and is
              not true: send_step stops such an enrollment permanently, and
              nothing re-arms it. Find the address first, on Sourcing. */}
          {withoutEmail.length
            ? `${withoutEmail.length} of ${chosen.length} have no email — enrolling them stops ` +
              `on the first send, permanently. Find their addresses on Sourcing first.`
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

/**
 * Which project this sequence belongs to.
 *
 * It decides two things and was previously visible in neither: the role that
 * fills {{ role_title }} and friends, and whether the sequence appears on the
 * Sequences page, which filters by the project in the sidebar. Set only at
 * create time until now -- so a sequence that landed in the wrong project was
 * both invisible and unfixable. PATCH /v1/sequences/{id} already took and
 * org-validated project_id; nothing was calling it.
 */
function ProjectPicker({ meta, setMeta, projects }) {
  return (
    <div className="card row wrap small muted" style={{ alignItems: 'center' }}>
      <span>Project</span>
      <select
        style={{ width: 300 }}
        data-testid="sequence-project-picker"
        value={meta.project_id ?? ''}
        onChange={(event) => setMeta({ ...meta, project_id: event.target.value || null })}
      >
        {/* Kept selectable: a sequence can legitimately have no project, and
            hiding the state a row is actually in reads as a broken picker. */}
        <option value="">No project</option>
        {projects.map((project) => (
          <option key={project.id} value={project.id}>
            {project.name}
          </option>
        ))}
      </select>
      <span>
        {meta.project_id
          ? "Its role fills {{ role_title }}, and the sequence shows under this project."
          : 'A sequence in no project is hidden from every project view.'}
      </span>
    </div>
  );
}

/**
 * Which mailbox this sequence sends from. Sequence-level, not per-step: a
 * conversation that changes address mid-thread arrives as a second email from
 * a stranger, so a step can never pick its own sender.
 */
function SenderPicker({ meta, setMeta, mailboxes }) {
  const active = (mailboxes.data?.connections ?? []).filter(
    (connection) => connection.status === 'active' && connection.send_enabled,
  );
  const value = meta.sender_mode === 'fixed' && meta.mailbox_id ? meta.mailbox_id : 'rotate';
  // An empty list is "every mailbox", which is what an untouched sequence
  // carries -- so a mailbox connected tomorrow joins this rotation without
  // anyone reopening the sequence. Ticking every box stores it that way again.
  const chosen = meta.mailbox_ids ?? [];
  const rotating = active.filter((c) => !chosen.length || chosen.includes(c.id));
  const capacity = rotating.reduce((total, c) => total + c.daily_send_cap, 0);

  function toggle(id) {
    const on = new Set(rotating.map((c) => c.id));
    if (on.has(id)) on.delete(id);
    else on.add(id);
    const next = active.filter((c) => on.has(c.id)).map((c) => c.id);
    setMeta({ ...meta, mailbox_ids: next.length === active.length ? [] : next });
  }

  return (
    <div className="card stack small muted">
      <div className="row wrap" style={{ alignItems: 'center' }}>
        <span>Send from</span>
        <select
          style={{ width: 300 }}
          data-testid="sender-picker"
          value={value}
          onChange={(event) => {
            const next = event.target.value;
            setMeta(
              next === 'rotate'
                ? { ...meta, sender_mode: 'rotate', mailbox_id: null }
                : // A pinned sequence has no pool to narrow, so drop the
                  // subset rather than leave it to reappear on un-pinning.
                  { ...meta, sender_mode: 'fixed', mailbox_id: next, mailbox_ids: [] },
            );
          }}
        >
          <option value="rotate">
            {active.length
              ? `Rotate across ${
                  chosen.length ? `${rotating.length} of ${active.length}` : 'all'
                } mailboxes (${capacity}/24h)`
              : 'Rotate across all mailboxes'}
          </option>
          {active.map((connection) => (
            <option key={connection.id} value={connection.id}>
              Only {connection.address} ({providerLabel(connection.provider)})
            </option>
          ))}
        </select>
        {active.length ? (
          <span>
            {value === 'rotate'
              ? 'Each contact stays on whichever mailbox emailed them first.'
              : 'Every step goes out from this one address.'}
          </span>
        ) : (
          <span>
            <Link to="/settings">Connect a mailbox</Link> to send as yourself
          </span>
        )}
      </div>

      {value === 'rotate' && active.length > 1 ? (
        <div className="row wrap" data-testid="rotation-mailboxes" style={{ gap: 14 }}>
          {active.map((connection) => {
            const on = rotating.some((c) => c.id === connection.id);
            return (
              <label
                key={connection.id}
                className="row"
                style={{ alignItems: 'center', gap: 6 }}
                title={`${providerLabel(connection.provider)} · ${connection.daily_send_cap}/24h`}
              >
                <input
                  type="checkbox"
                  checked={on}
                  // Something has to send. Unticking the last one would save a
                  // sequence that can only ever defer.
                  disabled={on && rotating.length === 1}
                  onChange={() => toggle(connection.id)}
                />
                {connection.address}
              </label>
            );
          })}
          <span>
            {chosen.length
              ? 'Only these send for this sequence.'
              : 'All of them, including any mailbox you connect later.'}
          </span>
        </div>
      ) : null}
    </div>
  );
}

export function SequenceEditorPage() {
  const { sequenceId } = useParams();
  const navigate = useNavigate();
  const { projects } = useCurrentProject();
  const sequence = useAsync(() => api.getSequence(sequenceId), [sequenceId]);
  const enrollments = useAsync(() => api.listEnrollments(sequenceId), [sequenceId]);
  const candidates = useAsync(() => api.listCandidates({ limit: 25 }), []);
  const mailboxes = useAsync(() => api.mailboxes(), []);

  const [meta, setMeta] = useState(null);
  const [steps, setSteps] = useState([]);
  const [selected, setSelected] = useState(0);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [needsPause, setNeedsPause] = useState(false);
  const [preview, setPreview] = useState(null);
  const [previewing, setPreviewing] = useState(false);
  // Monotonic request id and the (candidate, step) the current preview belongs
  // to -- the same pattern Sourcing.jsx uses for search.
  const previewRequest = useRef(0);
  const previewKey = useRef('');
  const [previewCandidate, setPreviewCandidate] = useState('');
  const [picking, setPicking] = useState(false);
  const [enrolling, setEnrolling] = useState(false);
  const [enrollError, setEnrollError] = useState(null);
  // People the last enrol request left out because they replied to us
  // recently; "Enrol anyway" re-sends exactly these ids with force.
  const [skippedRepliers, setSkippedRepliers] = useState([]);
  // The one enrolment a ReenrolPanel is open for, or null. One at a time on
  // purpose: this list is where you undo a stop for a person you can see,
  // not where you re-run a batch -- the Outbox's Cancelled view does that.
  const [reenrolling, setReenrolling] = useState(null);
  const bodyRef = useRef(null);
  const subjectRef = useRef(null);
  const lastFocused = useRef('body');
  // Read inside the reload effect, which must not see a stale `dirty`.
  const dirtyRef = useRef(false);
  const spam = useSpamCheck(steps, {
    trackOpens: meta?.track_opens ?? false,
    trackClicks: meta?.track_clicks ?? false,
  });

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
      track_opens: sequence.data.track_opens ?? false,
      track_clicks: sequence.data.track_clicks ?? false,
      from_email: sequence.data.from_email,
      from_name: sequence.data.from_name,
      sender_mode: sequence.data.sender_mode,
      mailbox_id: sequence.data.mailbox_id,
      project_id: sequence.data.project_id,
      mailbox_ids: sequence.data.mailbox_ids ?? [],
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
      ? [
          'name',
          'personalize',
          'stop_on_reply',
          'track_opens',
          'track_clicks',
          'from_email',
          'from_name',
          'sender_mode',
          'mailbox_id',
          'project_id',
        ].some((key) => meta[key] !== sequence.data[key]) ||
        // A list compares by identity, so it needs its own check or every
        // render would read as dirty.
        JSON.stringify(meta.mailbox_ids ?? []) !==
          JSON.stringify(sequence.data.mailbox_ids ?? [])
      : false;
  const dirty = stepsDirty || metaDirty;
  dirtyRef.current = dirty;
  const schedule = useMemo(() => schedulePreview(steps), [steps]);
  // Live rather than on-save: the recruiter is looking at the box they typed it
  // into. Saving a half-written step stays allowed -- the server is what refuses
  // to send -- so this warns and never blocks.
  const broken = useMemo(
    () => steps.map((step, index) => ({ index, found: malformedInStep(step) })).filter((s) => s.found.length),
    [steps],
  );

  if (sequence.loading || !meta) return <Loading label="Loading sequence…" />;
  if (sequence.error) return <ErrorBox error={sequence.error} />;

  const status = sequence.data.status;

  function updateStep(index, patch) {
    setSteps((previous) => previous.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }

  function insertVariable(snippet) {
    // Follow-up steps have no subject box, so a chip there can only mean the
    // body -- without this it would type into step 1's hidden template.
    if (lastFocused.current !== 'subject' || selected !== 0) {
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

  async function enrollCandidates(candidateIds, sendNow = false, force = false) {
    setEnrolling(true);
    setEnrollError(null);
    setNotice('');
    setSkippedRepliers([]);
    try {
      // One request: with send_now the backend sends to exactly the people
      // being enrolled before responding, so the returned rows already show
      // the post-send state (current_step advanced, next = the follow-up).
      // The old flow fired a second unbounded /run over the whole sequence,
      // which could outlive the proxy timeout and leave this button stuck on
      // "Enrolling…" while mail actually went out.
      const { enrollments: rows = [], skipped = [] } = await api.enroll(sequenceId, {
        candidate_ids: candidateIds,
        project_id: sequence.data.project_id ?? null,
        send_now: sendNow,
        force,
      });
      const enrolled = rows.length;
      const sentNow = rows.filter((row) => row.current_step > 0).length;
      await enrollments.reload();
      setPicking(false);
      let text = '';
      if (!enrolled) {
        text = '';
      } else if (status !== 'active') {
        text = `Enrolled ${enrolled}. Activate the sequence to start sending.`;
      } else if (sendNow) {
        // Sends leave one a minute, so "send now" to a batch sends the first
        // and queues the rest behind it.
        const waiting = enrolled - sentNow;
        text = sentNow
          ? `Enrolled ${enrolled} — ${sentNow} email${sentNow === 1 ? '' : 's'} sent${
              waiting === 1
                ? ', the other goes out in a minute'
                : waiting > 1
                  ? `, the other ${waiting} go out one a minute`
                  : ''
            }.`
          : `Enrolled ${enrolled} — sending within a minute, then one a minute.`;
      } else {
        text = `Enrolled ${enrolled} — first email goes out at the step's scheduled time.`;
      }
      if (skipped.length) {
        // A person who wrote back is in a conversation, not on a list. Say
        // who was left out and why, and let the recruiter overrule it.
        const names = skipped.map((row) => row.full_name || row.candidate_id).join(', ');
        const days = skipped[0].quiet_days;
        text = `${text ? `${text} ` : ''}Skipped ${skipped.length} who replied in the last ${days} days: ${names}.`;
        setSkippedRepliers(skipped.map((row) => ({ id: row.candidate_id, sendNow })));
      }
      setNotice(text);
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
    // Previewing the same candidate and step twice should not re-render, let
    // alone re-request: the server may have to write copy for it.
    const key = `${candidateId}:${selected}`;
    if (previewKey.current === key && preview) return;
    if (previewing) return;

    const seq = (previewRequest.current += 1);
    setError(null);
    setPreviewing(true);
    try {
      const result = await api.previewSequence(sequenceId, {
        candidate_id: candidateId,
        step_order: selected,
      });
      // A slower earlier request must not overwrite a newer one.
      if (previewRequest.current !== seq) return;
      previewKey.current = key;
      setPreview(result);
    } catch (caught) {
      if (previewRequest.current === seq) setError(caught);
    } finally {
      if (previewRequest.current === seq) setPreviewing(false);
    }
  }

  const step = steps[selected] ?? steps[0];
  // What a follow-up will go out as. Mirrors reply_subject() on the send side,
  // which reads the subject off step 1's sent message rather than re-rendering
  // it -- the variables are still unresolved here, so this shows the template.
  const rootSubject = (steps[0]?.subject_template ?? '').trim();
  const followUpSubject = rootSubject
    ? rootSubject.toLowerCase().startsWith('re:')
      ? rootSubject
      : `Re: ${rootSubject}`
    : 'set a subject on step 1';

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
      {notice ? (
        <div className="card small row spread" data-testid="enrol-notice">
          <span>{notice}</span>
          {skippedRepliers.length ? (
            <button
              type="button"
              className="tiny"
              disabled={enrolling}
              onClick={() =>
                enrollCandidates(
                  skippedRepliers.map((row) => row.id),
                  skippedRepliers[0].sendNow,
                  true,
                )
              }
            >
              Enrol anyway
            </button>
          ) : null}
        </div>
      ) : null}
      {broken.length ? (
        <div className="card small stack" data-testid="malformed-placeholders">
          <strong>
            {broken.length === 1 ? 'A placeholder' : 'Placeholders'} the renderer cannot read
          </strong>
          {broken.map(({ index, found }) => (
            <div key={index}>
              Step {index + 1}:{' '}
              {found.map((text, i) => (
                <span key={text}>
                  {i ? ', ' : ''}
                  <code>{text}</code>
                </span>
              ))}
            </div>
          ))}
          <span className="muted">
            A variable name can only use letters, numbers, underscores and dots — a
            space makes it unreadable, and it would send as those literal braces.
            Use the chips above (for a name, {'{{ first_name | there }}'}). These
            steps will not send until they are fixed.
          </span>
        </div>
      ) : null}

      <ProjectPicker meta={meta} setMeta={setMeta} projects={projects} />
      <SenderPicker meta={meta} setMeta={setMeta} mailboxes={mailboxes} />

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
                previewKey.current = '';
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
              {spam[index] ? (
                <div className="small" style={{ marginTop: 4 }}>
                  <SpamBadge report={spam[index]} />
                </div>
              ) : null}
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
              {/* Up top, not only under the body: the score should be the
                  first thing seen on a step, not something scrolled to. */}
              {spam[selected] ? (
                <div className="row spread wrap" style={{ gap: 8 }}>
                  <strong className="small">Step {selected + 1}</strong>
                  <SpamBadge report={spam[selected]} large />
                </div>
              ) : null}
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
              {/* Only step 1 owns a subject. Every later step replies on its
                  thread, and Gmail and Outlook both split a conversation the
                  moment the subject changes -- so a box here was an invitation
                  to break threading with a one-word edit. The line below shows
                  what the follow-up will actually go out as. */}
              {selected === 0 ? (
                <input
                  ref={subjectRef}
                  type="text"
                  placeholder="Subject"
                  value={step.subject_template}
                  onFocus={() => {
                    lastFocused.current = 'subject';
                  }}
                  onChange={(event) =>
                    updateStep(selected, { subject_template: event.target.value })
                  }
                />
              ) : (
                <div className="muted small">
                  Subject: <strong>{followUpSubject}</strong> — follow-ups continue step 1&apos;s
                  thread.
                </div>
              )}
              <EmailBodyEditor
                key={selected}
                ref={bodyRef}
                value={step.body_template}
                onFocus={() => {
                  lastFocused.current = 'body';
                }}
                onChange={(html) => updateStep(selected, { body_template: html })}
              />
              <SpamPanel report={spam[selected]} />
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
                <button
                  type="button"
                  className="tiny"
                  data-testid="preview-button"
                  disabled={dirty || previewing}
                  onClick={runPreview}
                >
                  {previewing ? 'Previewing…' : 'Preview'}
                </button>
              </div>
            </div>
            {dirty ? <div className="muted small">Save your changes to preview them.</div> : null}
            {preview ? (
              <>
                {preview.warnings?.length ? (
                  <div className="card small" data-testid="preview-warnings">
                    This step will not send: {preview.warnings.join(', ')}{' '}
                    {preview.warnings.length === 1 ? 'is' : 'are'} not a placeholder
                    the renderer can read, so the braces below are what the
                    candidate would have seen.
                  </div>
                ) : null}
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
                  {preview.spam ? <SpamBadge report={preview.spam} /> : null}
                </div>
              </>
            ) : null}
          </div>

          <div className="card stack">
            <div className="spread wrap" style={{ flexWrap: 'wrap' }}>
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
              <div className="row wrap small muted sequence-switches">
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
                {/* Off sends the email exactly as written: no hidden pixel,
                    links untouched. Both are bulk-mail signals to spam
                    filters; the cost is open / click counts in Analytics. */}
                <label title="Adds a hidden 1x1 image to count opens. Filters treat it as a bulk-mail signal.">
                  <input
                    type="checkbox"
                    data-testid="track-opens"
                    checked={meta.track_opens}
                    onChange={(event) => setMeta({ ...meta, track_opens: event.target.checked })}
                  />{' '}
                  Track opens
                </label>
                <label title="Routes links through a click tracker. Filters see a redirect instead of your link.">
                  <input
                    type="checkbox"
                    data-testid="track-clicks"
                    checked={meta.track_clicks}
                    onChange={(event) => setMeta({ ...meta, track_clicks: event.target.checked })}
                  />{' '}
                  Track clicks
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

            {reenrolling ? (
              <ReenrolPanel
                rows={[
                  {
                    enrollment_id: reenrolling.id,
                    candidate_id: reenrolling.candidate_id,
                    candidate_name: reenrolling.candidate?.full_name ?? reenrolling.candidate_id,
                  },
                ]}
                onClose={() => setReenrolling(null)}
                onDone={async (text) => {
                  setNotice(text);
                  await enrollments.reload();
                }}
              />
            ) : null}

            {enrollments.loading ? (
              <Loading label="Loading enrollments…" />
            ) : (enrollments.data ?? []).length ? (
              <EnrollmentList
                enrollments={enrollments.data}
                trailer={enrollmentTrailer}
                canReenrol={canReenrol}
                onReenrol={setReenrolling}
                reenrollingId={reenrolling?.id}
                stepCount={steps.length}
              />
            ) : (
              <Empty>Nobody enrolled yet — use “Add people” to enrol candidates.</Empty>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
