import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Empty, ErrorBox, Loading } from '../../components/common.jsx';
import { ReviewReason } from '../../components/ReviewReason.jsx';
import { formatComp, formatTimeline, intakeApi } from '../../lib/intake.js';
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

function CallCard({ row }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="card stack" data-testid="intake-call-row">
      <button type="button" className="option" onClick={() => setOpen((value) => !value)}>
        <span className="thread-head">
          <span className="thread-subject">
            {open ? '▾' : '▸'} {row.from_number || 'unknown caller'}
          </span>
          <span className="thread-meta muted small">
            {when(row.started_at || row.created_at)}
          </span>
        </span>
      </button>
      <div className="row spread">
        <div className="row" style={{ gap: 8 }}>
          <Badge tone={row.review_needed ? 'partial' : ''}>{row.status}</Badge>
          <span className="small muted">
            {row.gate_mode}
            {row.minutes_available != null ? ` · ${row.minutes_available} min` : ''}
            {row.required_complete ? '' : ' · gate open'}
          </span>
        </div>
        {row.candidate_id ? (
          <Link to={`/contacts/${row.candidate_id}`}>Open record</Link>
        ) : (
          <span className="muted small">unattributed</span>
        )}
      </div>
      {row.review_needed ? (
        <ReviewReason reason={row.review_reason} />
      ) : null}
      {open ? <CallDetail callId={row.id} /> : null}
    </div>
  );
}

export function IntakeCalls({ reviewNeeded, title } = {}) {
  const calls = useAsync(
    () => intakeApi.listCalls(reviewNeeded ? { review_needed: true } : {}),
    [reviewNeeded],
  );

  const rows = calls.data ?? [];

  return (
    <div className="stack" data-testid={reviewNeeded ? 'intake-review' : 'intake-calls'}>
      {title ? <h2>{title}</h2> : null}
      {calls.loading ? <Loading label="Loading calls…" /> : null}
      <ErrorBox error={calls.error} />
      {!calls.loading && !rows.length ? (
        <Empty>
          {reviewNeeded
            ? 'Nothing waiting on review.'
            : 'No voice intake calls yet. Text conversations are under the Text tab.'}
        </Empty>
      ) : (
        <div className="stack" style={{ gap: 8 }}>
          {rows.map((row) => (
            <CallCard key={row.id} row={row} />
          ))}
        </div>
      )}
    </div>
  );
}
