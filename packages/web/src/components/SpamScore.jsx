// Spam-risk score for sequence steps (backend: outreach/spamcheck.py).
//
// The editor scores unsaved copy, so the check runs on the steps as typed,
// debounced, against POST /v1/sequences/spam-check -- a pure endpoint that
// reads nothing from the database. Delivery findings (the tracking pixel,
// redirected links) are shown apart from the copy's own: the sequence's
// Track opens / Track clicks switches control them, not the wording.
import { useEffect, useRef, useState } from 'react';

import { api } from '../lib/api.js';
import { Badge } from './common.jsx';

const TONE = { low: 'strong', medium: 'partial', high: 'miss' };
const LABEL = { low: 'Low', medium: 'Medium', high: 'High' };

export function useSpamCheck(steps, { delay = 500, trackOpens = true, trackClicks = true } = {}) {
  const [results, setResults] = useState([]);
  const request = useRef(0);
  const payload = JSON.stringify({
    steps: (steps ?? []).map((step) => ({
      subject_template: step.subject_template ?? '',
      body_template: step.body_template ?? '',
    })),
    track_opens: trackOpens,
    track_clicks: trackClicks,
  });

  useEffect(() => {
    const id = ++request.current;
    const timer = setTimeout(async () => {
      try {
        const { results: next = [] } = await api.spamCheck(JSON.parse(payload));
        if (id === request.current) setResults(next);
      } catch {
        // Advice, not a gate: a failed check just leaves the last score up.
      }
    }, delay);
    return () => clearTimeout(timer);
  }, [payload, delay]);

  return results;
}

export function SpamBadge({ report }) {
  if (!report) return null;
  return (
    <Badge tone={TONE[report.level]}>
      <span data-testid="spam-badge">
        Spam risk: {LABEL[report.level]} · {report.score}
      </span>
    </Badge>
  );
}

function Finding({ finding }) {
  return (
    <li className="small" style={{ marginBottom: 6 }}>
      <strong>{finding.title}</strong>{' '}
      <span className="muted">+{finding.points}</span>
      {finding.matches?.length ? (
        <>
          {' — '}
          {finding.matches.map((text, i) => (
            <span key={text}>
              {i ? ', ' : ''}
              <code>{text}</code>
            </span>
          ))}
        </>
      ) : null}
      <div className="muted">{finding.detail}</div>
      {finding.fix ? <div>{finding.fix}</div> : null}
    </li>
  );
}

export function SpamPanel({ report }) {
  const [open, setOpen] = useState(false);
  if (!report) return null;
  const copy = report.findings.filter((f) => f.area !== 'delivery');
  const delivery = report.findings.filter((f) => f.area === 'delivery');
  return (
    <div className="card stack" data-testid="spam-panel" style={{ gap: 6 }}>
      <div className="spread">
        <div className="row">
          <SpamBadge report={report} />
          <span className="muted small">
            copy {report.content_score} · sending setup {report.delivery_score} (lower is better)
          </span>
        </div>
        {report.findings.length ? (
          <button type="button" className="tiny ghost" onClick={() => setOpen((v) => !v)}>
            {open ? 'Hide details' : `Why? (${report.findings.length})`}
          </button>
        ) : null}
      </div>
      {open ? (
        <>
          {copy.length ? (
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {copy.map((f) => (
                <Finding key={f.code} finding={f} />
              ))}
            </ul>
          ) : (
            <div className="small muted">Nothing in the copy itself looks risky.</div>
          )}
          {delivery.length ? (
            <>
              <div className="small muted">How it is sent (the Track opens / Track clicks switches, not the wording):</div>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {delivery.map((f) => (
                  <Finding key={f.code} finding={f} />
                ))}
              </ul>
            </>
          ) : null}
          <div className="small muted">
            A checklist of common filter signals with sample values in place of
            variables — a guide, not a guarantee of where the email lands.
          </div>
        </>
      ) : null}
    </div>
  );
}
