import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { api } from '../lib/api.js';
import {
  CHECK_TONE,
  HEALTH,
  INTERVALS,
  PLACEMENT,
  SEED_PROVIDERS,
  inboxRate,
  pct,
  seedSummary,
  when,
} from '../lib/deliverability.js';
import { useAsync } from '../lib/useAsync.js';

/**
 * Where outreach lands, per sending domain and per mailbox.
 *
 * Step 1 of the spam work: measurement only. Seed tests re-send one real email
 * from each mailbox to inboxes we own and read where it landed; the rest is
 * what real traffic says (reply rate, bounces, policy blocks) and the
 * domain's DNS. Nothing on this page pauses or throttles sending.
 */

const OFF_REASONS = {
  mailbox_inactive: 'it is disconnected or erroring',
  send_disabled: 'sending is switched off for it',
  capped_off: 'its daily cap is 0',
  provider_unconfigured: 'this server has no credentials for its provider',
};

const providerName = (label) =>
  SEED_PROVIDERS.find((p) => p.value === label)?.label.replace(' (IMAP)', '') ?? label;

function Tag({ tone = '', children, ...rest }) {
  // Badge in common.jsx drops extra props, and the specs need test ids.
  return (
    <span className={`badge ${tone}`} {...rest}>
      {children}
    </span>
  );
}

function HealthBadge({ health, testid }) {
  const meta = HEALTH[health?.label] ?? HEALTH.no_data;
  return (
    <Tag tone={meta.tone} data-testid={testid} title={(health?.reasons ?? []).join('\n')}>
      {meta.text}
    </Tag>
  );
}

/** Weekly reply rate as small bars; empty weeks are a dot, not a zero bar. */
function Sparkline({ weekly, label }) {
  const rates = (weekly ?? []).map((w) => (w.people ? w.replied / w.people : null));
  const top = Math.max(0.05, ...rates.filter((r) => r !== null));
  const width = 8 * rates.length;
  return (
    <svg
      className="dl-spark"
      width={width}
      height="22"
      viewBox={`0 0 ${width} 22`}
      role="img"
      aria-label={label}
    >
      {rates.map((rate, i) => {
        const tip = weekly[i].people
          ? `Week of ${weekly[i].week}: ${weekly[i].replied}/${weekly[i].people} replied (${pct(rate)})`
          : `Week of ${weekly[i].week}: nobody first emailed`;
        if (rate === null) {
          return (
            <circle key={weekly[i].week} cx={i * 8 + 3} cy="20" r="1">
              <title>{tip}</title>
            </circle>
          );
        }
        const h = Math.max(2, (rate / top) * 20);
        return (
          <rect key={weekly[i].week} x={i * 8} y={22 - h} width="6" height={h} rx="1">
            <title>{tip}</title>
          </rect>
        );
      })}
    </svg>
  );
}

function SeedForm({ onAdded }) {
  const [draft, setDraft] = useState({
    provider_label: 'gmail',
    address: '',
    password: '',
    imap_host: '',
    imap_port: '993',
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const provider = SEED_PROVIDERS.find((p) => p.value === draft.provider_label);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.seedAdd({
        provider_label: draft.provider_label,
        address: draft.address.trim(),
        password: draft.password,
        imap_host: draft.imap_host.trim(),
        imap_port: Number.parseInt(draft.imap_port, 10) || 993,
      });
      setDraft((d) => ({ ...d, address: '', password: '' }));
      onAdded();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  const set = (key) => (event) => setDraft((d) => ({ ...d, [key]: event.target.value }));
  return (
    <form className="stack dl-seed-form" onSubmit={submit} data-testid="seed-form">
      <div className="row wrap">
        <select value={draft.provider_label} onChange={set('provider_label')} data-testid="seed-provider" aria-label="Seed provider">
          {SEED_PROVIDERS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
        <input
          type="email"
          placeholder="seed address"
          value={draft.address}
          onChange={set('address')}
          data-testid="seed-address"
          aria-label="Seed address"
          required
        />
        <input
          type="password"
          placeholder="app password"
          autoComplete="new-password"
          value={draft.password}
          onChange={set('password')}
          data-testid="seed-password"
          aria-label="App password"
          required
        />
        {draft.provider_label === 'other' ? (
          <>
            <input
              type="text"
              placeholder="IMAP host"
              value={draft.imap_host}
              onChange={set('imap_host')}
              data-testid="seed-host"
              aria-label="IMAP host"
              required
            />
            <input
              type="number"
              className="dl-port"
              value={draft.imap_port}
              onChange={set('imap_port')}
              aria-label="IMAP port"
            />
          </>
        ) : null}
        <button type="submit" className="primary" disabled={busy} data-testid="seed-submit">
          {busy ? 'Signing in…' : 'Sign in and add'}
        </button>
      </div>
      <div className="small muted">{provider?.help}</div>
      {error ? (
        <div className="error" data-testid="seed-form-error">
          {error.message}
        </div>
      ) : null}
    </form>
  );
}

function Seeds({ seeds, reload, setError }) {
  const [busy, setBusy] = useState('');

  async function act(seed, fn) {
    setBusy(seed.id);
    setError(null);
    try {
      await fn();
      reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="card stack" data-testid="seed-card">
      <div>
        <strong>Seed inboxes</strong>
        <div className="small muted">
          Inboxes you own at the providers whose filters you want to sample. Each sending mailbox
          re-sends one of its own recent emails to one of these, and the app looks in the inbox,
          the other tabs and spam. Seeds are only ever read: nothing is opened, moved or marked,
          because rescuing a test from spam would teach the filter to trust us.
        </div>
      </div>
      {seeds.length ? (
        <div className="dl-wrap">
        <table className="dgrid dl-table">
          <thead>
            <tr>
              <th>Seed</th>
              <th>Provider</th>
              <th>Status</th>
              <th className="num">Tests (7d)</th>
              <th>Last read</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {seeds.map((seed) => (
              <tr key={seed.id} data-testid="seed-row">
                <td>
                  {seed.address}
                  {seed.error ? (
                    <div className="small dl-error" data-testid="seed-error">
                      {seed.error}
                    </div>
                  ) : null}
                  {!seed.spam_visible && !seed.error ? (
                    <div className="small muted">No spam folder found: spam results cannot be seen here.</div>
                  ) : null}
                </td>
                <td>{providerName(seed.provider_label)}</td>
                <td>
                  <Tag tone={seed.status === 'active' ? 'strong' : seed.status === 'error' ? 'miss' : ''}>
                    {seed.status}
                  </Tag>
                </td>
                <td className="num">{seed.tests_7d}</td>
                <td className="small">{when(seed.last_checked_at) || '—'}</td>
                <td className="dl-actions">
                  <button
                    type="button"
                    className="tiny ghost"
                    disabled={busy === seed.id}
                    onClick={() => act(seed, () => api.seedCheck(seed.id))}
                  >
                    Read now
                  </button>
                  <button
                    type="button"
                    className="tiny ghost"
                    disabled={busy === seed.id}
                    onClick={() =>
                      act(seed, () =>
                        api.seedUpdate(seed.id, { status: seed.status === 'paused' ? 'active' : 'paused' }),
                      )
                    }
                  >
                    {seed.status === 'paused' ? 'Resume' : 'Pause'}
                  </button>
                  <button
                    type="button"
                    className="tiny ghost"
                    disabled={busy === seed.id}
                    onClick={() => {
                      if (window.confirm(`Remove ${seed.address}? Its test history goes with it.`)) {
                        act(seed, () => api.seedRemove(seed.id));
                      }
                    }}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : (
        <div className="small muted" data-testid="seed-empty">
          No seed inboxes yet. Add a few (Gmail and Yahoo to start) and tests begin on the next
          scheduled run, or press Test now on any mailbox below.
        </div>
      )}
      <SeedForm onAdded={reload} />
    </div>
  );
}

function Schedule({ settings, reload, setError }) {
  const [saving, setSaving] = useState(false);
  async function change(event) {
    setSaving(true);
    setError(null);
    try {
      await api.deliverabilitySettings({
        seed_test_interval_hours: Number.parseInt(event.target.value, 10),
      });
      reload();
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  }
  return (
    <div className="card spread">
      <div>
        <strong>Seed test schedule</strong>
        <div className="small muted">
          One test per mailbox per run. Each test counts toward that mailbox's daily cap and waits
          for sending hours and the gap between emails, like any other send.
        </div>
      </div>
      <select
        value={settings.seed_test_interval_hours}
        onChange={change}
        disabled={saving}
        data-testid="interval-select"
        aria-label="Seed test schedule"
      >
        {INTERVALS.map((option) => (
          <option key={option.hours} value={option.hours}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function Checks({ checks, checkedAt }) {
  if (!checks) {
    return <div className="small muted">DNS not checked yet — it runs within a few minutes of a mailbox sending.</div>;
  }
  const items = [
    { name: 'SPF', status: checks.spf?.status, note: checks.spf?.note || checks.spf?.record },
    {
      name: 'DMARC',
      status: checks.dmarc?.status,
      note: checks.dmarc?.policy ? `policy ${checks.dmarc.policy}` : '',
    },
    {
      name: 'DKIM',
      status: checks.dkim?.status,
      note: (checks.dkim?.selectors ?? [])
        .map((s) => `${s.selector}: ${s.bits ? `${s.bits}-bit` : 'unreadable key'}`)
        .join(', '),
    },
    { name: 'MX', status: checks.mx?.status, note: '' },
    ...(checks.blocklists ?? []).map((b) => ({ name: b.name, status: b.status, note: b.note })),
  ];
  const noteText = items
    .filter((i) => i.note && i.status !== 'ok' && i.status !== 'clean')
    .map((i) => `${i.name} — ${i.note}`)
    .join(' · ');
  return (
    <div className="stack dl-checks" data-testid="dns-checks">
      <div className="row wrap">
        {items.map((item) => (
          <Tag key={item.name} tone={CHECK_TONE[item.status] ?? ''} title={item.note || ''}>
            {item.name}: {item.status ?? '—'}
          </Tag>
        ))}
      </div>
      <div className="small muted">
        {noteText}
        {checkedAt ? `${noteText ? '. ' : ''}Checked ${when(checkedAt)}.` : ''}
      </div>
    </div>
  );
}

function MailboxRow({ mailbox, reload, setError, setNote }) {
  const [busy, setBusy] = useState(false);
  async function testNow() {
    setBusy(true);
    setError(null);
    try {
      const result = await api.seedTestNow(mailbox.id);
      if (!result.seeds) setNote('Add a seed inbox first.');
      else if (result.reason) setNote(`${mailbox.address} cannot send right now (${OFF_REASONS[result.reason] ?? result.reason}); nothing queued.`);
      else if (!result.room) setNote(`${mailbox.address} has no room left in its daily cap; nothing queued.`);
      else setNote(`Queued ${result.queued} test${result.queued === 1 ? '' : 's'} from ${mailbox.address}.`);
      reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }
  const last = mailbox.last_test;
  const placement = last ? PLACEMENT[last.status] ?? { text: last.status, tone: '' } : null;
  return (
    <tr data-testid="mailbox-row">
      <td>
        {mailbox.address}
        {mailbox.status !== 'active' || !mailbox.send_enabled ? (
          <div className="small muted">{mailbox.status !== 'active' ? mailbox.status : 'sending off'}</div>
        ) : null}
      </td>
      <td>
        <HealthBadge health={mailbox.health} testid="mailbox-health" />
        {mailbox.health?.reasons?.length ? (
          <div className="small muted dl-reasons">{mailbox.health.reasons[0]}</div>
        ) : null}
      </td>
      <td className="small" title={`inbox ${mailbox.seed.inbox}, other tab ${mailbox.seed.other_tab}, spam ${mailbox.seed.spam}, missing ${mailbox.seed.missing}, waiting ${mailbox.seed.pending}`}>
        {seedSummary(mailbox.seed)}
      </td>
      <td className="small">
        {placement ? (
          <>
            <Tag tone={placement.tone}>{placement.text}</Tag>{' '}
            <span className="muted">
              {providerName(last.seed_provider)} · {when(last.sent_at || last.created_at)}
            </span>
          </>
        ) : (
          '—'
        )}
      </td>
      <td className="num" title={`${mailbox.replied_recent} of ${mailbox.people_recent} first emailed 3–17 days ago`}>
        {pct(mailbox.reply_rate_recent)}
        <div className="small muted">{mailbox.people_recent} people</div>
      </td>
      <td>
        <Sparkline weekly={mailbox.weekly} label={`Weekly reply rate for ${mailbox.address}`} />
      </td>
      <td className="num">{pct(mailbox.bounce_rate_14d)}</td>
      <td className="num">{mailbox.blocks_14d}</td>
      <td className="num">{mailbox.daily_send_cap}</td>
      <td>
        <button type="button" className="tiny" disabled={busy} onClick={testNow} data-testid="test-now">
          Test now
        </button>
      </td>
    </tr>
  );
}

function DomainCard({ domain, reload, setError, setNote }) {
  const rate = inboxRate(domain.seed);
  return (
    <div className="card stack" data-testid="domain-card">
      <div className="spread">
        <div>
          <h2 className="dl-domain">
            {domain.consumer ? `Personal accounts (${domain.domain})` : domain.domain}{' '}
            <HealthBadge health={domain.health} testid="domain-health" />
          </h2>
          {domain.health?.reasons?.length ? (
            <div className="small muted">{domain.health.reasons.join(' · ')}</div>
          ) : null}
        </div>
        <Sparkline weekly={domain.weekly} label={`Weekly reply rate for ${domain.domain}`} />
      </div>
      <div className="grid four">
        <div className="dl-figure">
          <div className="value">{rate === null ? '—' : pct(rate, 0)}</div>
          <div className="label">Seed tests in inbox (7d)</div>
          <div className="note">{seedSummary(domain.seed)}</div>
        </div>
        <div className="dl-figure">
          <div className="value">{pct(domain.reply_rate_recent)}</div>
          <div className="label">Reply rate</div>
          <div className="note">
            {domain.replied_recent} of {domain.people_recent} first emailed 3–17 days ago
          </div>
        </div>
        <div className="dl-figure">
          <div className="value">{pct(domain.bounce_rate_14d)}</div>
          <div className="label">Bounce rate (14d)</div>
        </div>
        <div className="dl-figure">
          <div className="value">{domain.blocks_14d}</div>
          <div className="label">Policy blocks (14d)</div>
          <div className="note">Provider refused on reputation, not a dead address</div>
        </div>
      </div>
      {domain.consumer ? (
        <div className="small muted">
          No domain checks: gmail.com's records are Google's, and each personal account has its own
          reputation.
        </div>
      ) : (
        <Checks checks={domain.checks} checkedAt={domain.checked_at} />
      )}
      <div className="dl-wrap">
        <table className="dgrid dl-table">
          <thead>
            <tr>
              <th>Mailbox</th>
              <th>Health</th>
              <th>Seeds (7d)</th>
              <th>Last test</th>
              <th className="num">Reply</th>
              <th>Weekly</th>
              <th className="num">Bounce</th>
              <th className="num">Blocks</th>
              <th className="num">Cap</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {domain.mailboxes.map((mailbox) => (
              <MailboxRow key={mailbox.id} mailbox={mailbox} reload={reload} setError={setError} setNote={setNote} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RecentTests({ tests }) {
  if (!tests.length) return null;
  return (
    <div className="card stack" data-testid="recent-tests">
      <strong>Recent seed tests</strong>
      <div className="dl-wrap">
        <table className="dgrid dl-table">
          <thead>
            <tr>
              <th>From</th>
              <th>To seed</th>
              <th>Where it landed</th>
              <th>Subject</th>
              <th>Sent</th>
            </tr>
          </thead>
          <tbody>
            {tests.map((test) => {
              const placement = PLACEMENT[test.status] ?? { text: test.status, tone: '' };
              return (
                <tr key={test.id}>
                  <td>{test.mailbox_address}</td>
                  <td>{test.seed_address}</td>
                  <td>
                    <Tag tone={placement.tone}>{placement.text}</Tag>
                    {test.folder && test.status !== 'inbox' ? <span className="small muted"> {test.folder}</span> : null}
                    {test.error ? <div className="small muted">{test.error}</div> : null}
                  </td>
                  <td className="small dl-subject">{test.subject}</td>
                  <td className="small">{when(test.sent_at || test.created_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function DeliverabilityPage() {
  const overview = useAsync(() => api.deliverability(), []);
  const [error, setError] = useState(null);
  const [note, setNote] = useState('');
  const [rechecking, setRechecking] = useState(false);
  const data = overview.data;

  async function recheck() {
    setRechecking(true);
    setError(null);
    try {
      await api.domainsRecheck();
      overview.reload();
    } catch (err) {
      setError(err);
    } finally {
      setRechecking(false);
    }
  }

  return (
    <div className="stack" data-testid="deliverability-page">
      <PageHeader
        title="Deliverability"
        subtitle="Where outreach lands, per domain and per mailbox. Measurement only: nothing here pauses or slows sending."
        actions={
          <button type="button" className="tiny" onClick={recheck} disabled={rechecking} data-testid="recheck-dns">
            {rechecking ? 'Checking DNS…' : 'Recheck DNS'}
          </button>
        }
      />
      <ErrorBox error={error || overview.error} />
      {note ? (
        <div className="card small" data-testid="deliverability-note">
          {note}
        </div>
      ) : null}
      {overview.loading && !data ? <Loading /> : null}
      {data ? (
        <>
          <Seeds seeds={data.seeds} reload={overview.reload} setError={setError} />
          <Schedule settings={data.settings} reload={overview.reload} setError={setError} />
          {data.domains.length ? (
            data.domains.map((domain) => (
              <DomainCard
                key={domain.domain}
                domain={domain}
                reload={overview.reload}
                setError={setError}
                setNote={setNote}
              />
            ))
          ) : (
            <Empty>
              <span data-testid="no-mailboxes">
                No sending mailboxes yet. <Link to="/settings">Connect one in Settings</Link> and
                its health shows up here.
              </span>
            </Empty>
          )}
          <RecentTests tests={data.recent_tests} />
        </>
      ) : null}
    </div>
  );
}
