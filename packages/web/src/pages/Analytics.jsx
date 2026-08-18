import { Empty, Loading, PageHeader, Stat } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAsync } from '../lib/useAsync.js';

const percent = (value) => `${((value ?? 0) * 100).toFixed(0)}%`;

export function AnalyticsPage() {
  const funnel = useAsync(() => api.funnel(), []);
  const activity = useAsync(() => api.activity(), []);
  const index = useAsync(() => api.indexStats(), []);

  if (funnel.loading) return <Loading />;

  const stages = funnel.data?.stages ?? {};
  const outreach = funnel.data?.outreach ?? {};

  return (
    <div className="stack">
      <PageHeader title="Analytics" subtitle="Pipeline health and outreach performance." />

      <div className="grid four">
        <Stat value={index.data?.documents ?? '—'} label="people indexed" />
        <Stat value={outreach.sent ?? 0} label="emails sent" />
        <Stat value={percent(funnel.data?.open_rate)} label="open rate" />
        <Stat value={percent(funnel.data?.reply_rate)} label="reply rate" />
      </div>

      <div className="card">
        <strong className="small">Pipeline by stage</strong>
        <div className="facets" style={{ marginTop: 10 }}>
          {Object.entries(stages).map(([stage, count]) => (
            <span className="badge" key={stage}>
              {stage}: {count}
            </span>
          ))}
        </div>
      </div>

      <div className="card">
        <strong className="small">Outreach</strong>
        <div className="grid four" style={{ marginTop: 10 }}>
          <Stat value={outreach.opened ?? 0} label="opened" />
          <Stat value={outreach.clicked ?? 0} label="clicked" />
          <Stat value={outreach.replied ?? 0} label="replied" />
          <Stat value={outreach.positive_replies ?? 0} label="positive" />
        </div>
      </div>

      <div className="card">
        <strong className="small">Recent activity</strong>
        {activity.data?.length ? (
          <div className="stack" style={{ marginTop: 10 }}>
            {activity.data.map((event) => (
              <div key={event.id} className="small">
                <span className="mono muted">{new Date(event.created_at).toLocaleString()}</span>{' '}
                <span className="badge">{event.type}</span>{' '}
                <span className="muted mono">{event.subject_id}</span>
              </div>
            ))}
          </div>
        ) : (
          <Empty>No activity recorded yet.</Empty>
        )}
      </div>
    </div>
  );
}
