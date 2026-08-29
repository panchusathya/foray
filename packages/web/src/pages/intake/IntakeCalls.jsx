import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../../components/common.jsx';
import { ReviewReason } from '../../components/ReviewReason.jsx';
import { groupCalls } from '../../lib/intakeGroups.js';
import { formatComp, formatTimeline, intakeApi } from '../../lib/intake.js';
import { formatPhone } from '../../lib/phone.js';
import { useAsync } from '../../lib/useAsync.js';

function when(value) {
  return value ? new Date(value).toLocaleString() : '—';
}

function ExtractedSummary({ extracted }) {
  if (!extracted || !Object.keys(extracted).length) {
    return <div className="small muted">No extraction yet.</div>;
  }
  const spec = extracted.spec || {};
  const titles = (spec.job_titles || []).join(', ') || '—';
  return (
    <div className="stack" style={{ gap: 6 }} data-testid="intake-call-summary">
      <div className="grid three">
        <div>
          <div className="small muted">Timeline</div>
          <div>{formatTimeline(extracted.timeline)}</div>
        </div>
        <div>
          <div className="small muted">Target comp</div>
          <div>{formatComp(extracted.target_total || extracted.target_base)}</div>
        </div>
        <div>
          <div className="small muted">Work auth</div>
          <div>{extracted.work_authorization || '—'}</div>
        </div>
      </div>
      <div>
        <div className="small muted">Roles</div>
        <div>{titles}</div>
      </div>
      {extracted.employment_status ? (
        <div className="small muted">Employment: {extracted.employment_status}</div>
      ) : null}
    </div>
  );
}

function CallDetail({ callId }) {
  const detail = useAsync(() => intakeApi.getCall(callId), [callId]);
  if (detail.loading) return <Loading label="Loading transcript…" />;
  if (detail.error) return <ErrorBox error={detail.error} />;
  const call = detail.data;
  return (
    <div className="stack" style={{ gap: 10 }} data-testid="intake-call-detail">
      <ExtractedSummary extracted={call.extracted} />
      <div>
        <div className="small muted">Transcript</div>
        {call.transcript?.trim() ? (
          <pre className="message-body" data-testid="intake-call-transcript">
            {call.transcript}
          </pre>
        ) : (
          <div className="small muted">No transcript captured yet.</div>
        )}
      </div>
      {call.recording_url ? (
        <div className="small">
          <a href={call.recording_url} target="_blank" rel="noreferrer">
            Open recording →
          </a>
        </div>
      ) : null}
    </div>
  );
}

/** One call inside a caller's group. The advisory blocks live here, not on the
 *  group header -- hoisting them would undo the consolidation. */
function CallRow({ row }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="stack" style={{ gap: 6 }} data-testid="intake-call-row">
      <div className="row spread">
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          <button type="button" className="tiny ghost" onClick={() => setOpen((value) => !value)}>
            {open ? '▾' : '▸'} {when(row.started_at || row.created_at)}
          </button>
          <Badge tone={row.review_needed ? 'partial' : ''}>{row.status}</Badge>
          <span className="small muted">
            {row.gate_mode}
            {row.minutes_available != null ? ` · ${row.minutes_available} min` : ''}
            {row.required_complete ? '' : ' · gate open'}
          </span>
        </div>
      </div>
      {row.review_needed ? <ReviewReason reason={row.review_reason} /> : null}
      {open ? <CallDetail callId={row.id} /> : null}
    </div>
  );
}

function CallerCard({ group }) {
  const [open, setOpen] = useState(false);
  const number = formatPhone(group.number);
  // What they called themselves beats the number they called from. A phone
  // number is the last resort, not the headline -- a queue of numbers tells a
  // recruiter nothing about who to ring.
  const who = group.contactName || group.contactEmail || number || 'unknown caller';
  const count = group.calls.length;
  return (
    <div className="card stack" data-testid="intake-caller">
      <button type="button" className="option" onClick={() => setOpen((value) => !value)}>
        <span className="thread-head">
          <span className="thread-subject">
            {open ? '▾' : '▸'} {who}
          </span>
          <span className="thread-meta muted small">
            {count} call{count === 1 ? '' : 's'} · {when(group.latest)}
          </span>
        </span>
      </button>
      <div className="row spread">
        <div className="small muted">
          {[number, group.contactEmail].filter(Boolean).join(' · ')}
          {group.reviewCount ? ` · ${group.reviewCount} needing review` : ''}
        </div>
        {group.candidateId ? (
          <Link to={`/contacts/${group.candidateId}`}>Open record</Link>
        ) : (
          <span className="muted small">unattributed</span>
        )}
      </div>
      {open ? (
        <div className="stack" style={{ gap: 10 }}>
          {group.calls.map((row) => (
            <CallRow key={row.id} row={row} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function IntakeCalls({ reviewNeeded, title } = {}) {
  const calls = useAsync(
    () => intakeApi.listCalls(reviewNeeded ? { review_needed: true } : {}),
    [reviewNeeded],
  );

  const groups = groupCalls(calls.data);

  return (
    <div className="stack" data-testid={reviewNeeded ? 'intake-review' : 'intake-calls'}>
      {title ? <h2>{title}</h2> : null}
      {calls.loading ? <Loading label="Loading calls…" /> : null}
      <ErrorBox error={calls.error} />
      {!calls.loading && !groups.length ? (
        <Empty>
          {reviewNeeded
            ? 'Nothing waiting on review.'
            : 'No voice intake calls yet. Text conversations are under the Text tab.'}
        </Empty>
      ) : (
        <div className="stack" style={{ gap: 8 }}>
          {groups.map((group) => (
            <CallerCard key={group.key} group={group} />
          ))}
        </div>
      )}
    </div>
  );
}
