/**
 * Home dashboard (light version — the charted version lands with Track U3).
 * KPI row over the funnel endpoint, quick actions, and recent activity.
 */

import { Link } from 'react-router-dom';

import { Empty, ErrorBox, Loading, PageHeader, Stat } from '../components/common.jsx';
import { api } from '../lib/api.js';
import { useAuth } from '../lib/auth.jsx';
import { useCurrentProject } from '../lib/project.jsx';
import { useAsync } from '../lib/useAsync.js';

function firstName(user) {
  const name = user?.name?.trim() || user?.email || '';
  return name.split(/[\s@]/)[0] || 'there';
}

function percent(rate) {
  return `${Math.round((rate ?? 0) * 100)}%`;
}

const EVENT_LABELS = {
  'search.run': 'Search run',
  'agent.run': 'Agent run',
  'outreach.sent': 'Email sent',
  'outreach.opened': 'Email opened',
  'outreach.clicked': 'Link clicked',
  'outreach.replied': 'Reply received',
};

export function HomePage() {
  const { user } = useAuth();
  const { project } = useCurrentProject();
  const funnel = useAsync(() => api.funnel(), []);
  const activity = useAsync(() => api.activity(), []);

  const stages = funnel.data?.stages ?? {};
  const outreach = funnel.data?.outreach ?? {};
  const shortlisted = Object.values(stages).reduce((sum, n) => sum + (n || 0), 0);

  return (
    <>
      <PageHeader
        title={`Hello, ${firstName(user)}`}
        subtitle="Quick glance at activity across projects, agents and outreach."
        actions={
          <Link to="/search">
            <button type="button" className="primary">
              New search
            </button>
          </Link>
        }
      />

      <div className="stack">
        <ErrorBox error={funnel.error} />
        {funnel.loading ? (
          <Loading label="Loading overview…" />
        ) : (
          <div className="grid four">
            <Stat value={shortlisted} label="Shortlisted" />
            <Stat value={outreach.sent ?? 0} label="Contacted" />
            <Stat
              value={outreach.replied ?? 0}
              label={`Replied · ${percent(funnel.data?.reply_rate)}`}
            />
            <Stat
              value={outreach.positive_replies ?? 0}
              label={`Interested · ${percent(funnel.data?.positive_reply_rate)}`}
            />
          </div>
        )}

        <div className="grid two">
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Get moving</h3>
            <div className="stack" style={{ gap: 8 }}>
              <Link to="/search" className="nav-link">
                Run a natural-language search
              </Link>
              <Link to={project ? `/projects/${project.id}` : '/projects'} className="nav-link">
                {project ? `Open ${project.name} pipeline` : 'Create your first project'}
              </Link>
              <Link to="/sequences" className="nav-link">
                Review outreach sequences
              </Link>
            </div>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Recent activity</h3>
            <ErrorBox error={activity.error} />
            {activity.loading ? (
              <Loading label="Loading activity…" />
            ) : activity.data?.length ? (
              <div className="stack" style={{ gap: 6 }}>
                {activity.data.slice(0, 8).map((event) => (
                  <div key={event.id} className="row small">
                    <span className="badge accent">{EVENT_LABELS[event.type] ?? event.type}</span>
                    <span className="muted mono">
                      {event.created_at ? new Date(event.created_at).toLocaleString() : ''}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <Empty>No activity yet — run a search to get started.</Empty>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
