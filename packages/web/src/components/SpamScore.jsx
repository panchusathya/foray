// Spam-risk score for sequence steps (backend: outreach/spamcheck.py).
//
// The editor scores unsaved copy, so the check runs on the steps as typed,
// debounced, against POST /v1/sequences/spam-check -- a pure endpoint that
// reads nothing from the database. Delivery findings (the tracking pixel,
// redirected links) are shown apart from the copy's own: the sequence's
// Track opens / Track clicks switches control them, not the wording.
import { useEffect, useRef, useState } from 'react';

import { api } from '../lib/api.js';
const LABEL = { low: 'Low', medium: 'Medium', high: 'High' };

export function useSpamCheck(steps, { delay = 500, trackOpens = false, trackClicks = false } = {}) {
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

export function SpamBadge({ report, large = false }) {
  if (!report) return null;
  return (
    <span className={`spam-chip ${report.level}${large ? ' large' : ''}`} data-testid="spam-badge">
      <span className="spam-dot" aria-hidden="true" />
      Spam risk: {LABEL[report.level]} · {report.score}
    </span>
  );
}

function Finding({ finding }) {
  return (
    <li className="spam-finding">
      <div className="row" style={{ gap: 6, alignItems: 'baseline' }}>
        <strong>{finding.title}</strong>
        <span className="muted small">+{finding.points}</span>
      </div>
      {finding.matches?.length ? (
        <div className="small">
          {finding.matches.map((text, i) => (
            <span key={text}>
              {i ? ', ' : ''}
              <code>{text}</code>
            </span>
          ))}
        </div>
      ) : null}
      <div className="muted small">{finding.detail}</div>
      {finding.fix ? <div className="small spam-fix">→ {finding.fix}</div> : null}
    </li>
  );
}

const HEADLINE = {
  low: 'Looks clean',
  medium: 'Some spam signals',
  high: 'Likely to be filtered',
};

/** The editor's spam card: big, coloured by risk, with the top issues shown. */
export function SpamPanel({ report }) {
  const [open, setOpen] = useState(false);
  if (!report) return null;
  const copy = report.findings.filter((f) => f.area !== 'delivery');
  const delivery = report.findings.filter((f) => f.area === 'delivery');
  const top = report.findings.slice(0, 2);
  const rest = report.findings.length - top.length;
  return (
    <section
      className={`spam-card ${report.level}`}
      data-testid="spam-panel"
      aria-label={`Spam risk ${LABEL[report.level]}, ${report.score} out of 100`}
    >
      <div className="spam-head">
        <div className="spam-score">
          <span className="spam-number">{report.score}</span>
          <span className="spam-outof">/100</span>
        </div>
        <div className="stack" style={{ gap: 4, flex: 1, minWidth: 0 }}>
          <div className="row spread" style={{ gap: 8 }}>
            <strong className="spam-title">
              Spam risk: {LABEL[report.level]} — {HEADLINE[report.level]}
            </strong>
            <span className="muted small">lower is better</span>
          </div>
          <div className="spam-meter" aria-hidden="true">
            <div style={{ width: `${Math.max(2, Math.min(100, report.score))}%` }} />
          </div>
          <div className="muted small">
            Copy {report.content_score} · Sending setup {report.delivery_score}
            {report.word_count ? ` · ${report.word_count} words` : ''}
          </div>
        </div>
      </div>
      {top.length ? (
        <ul className="spam-findings">
          {(open ? copy : top.filter((f) => f.area !== 'delivery')).map((f) => (
            <Finding key={f.code} finding={f} />
          ))}
          {(open ? delivery : top.filter((f) => f.area === 'delivery')).length ? (
            <li className="muted small spam-group">
              How it is sent (the Track opens / Track clicks switches, not the wording):
            </li>
          ) : null}
          {(open ? delivery : top.filter((f) => f.area === 'delivery')).map((f) => (
            <Finding key={f.code} finding={f} />
          ))}
        </ul>
      ) : (
        <div className="small">Nothing here looks like spam to a filter.</div>
      )}
      {rest > 0 || open ? (
        <button type="button" className="tiny ghost" onClick={() => setOpen((v) => !v)}>
          {open ? 'Show fewer' : `Show all ${report.findings.length} signals`}
        </button>
      ) : null}
      <div className="muted small">
        A checklist of common filter signals with sample values in place of variables — a
        guide, not a guarantee of where the email lands.
      </div>
    </section>
  );
}
