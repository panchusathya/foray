/**
 * Home: the four numbers, the list of what is stuck, what just happened, and
 * the projects roster.
 *
 * Everything on this page is a way *into* the app — there is no read-only
 * furniture left. The KPI tiles open the list they count, the activity rows
 * open the person they happened to, and "Waiting on you" is computed by
 * `/v1/analytics/todo` rather than being three hardcoded links that said the
 * same thing on a workspace with nothing to do and one with a week of unread
 * replies.
 */

import { Link } from 'react-router-dom';

import { Empty, ErrorBox, Loading, PageHeader, StatLink } from '../components/common.jsx';
import { ProjectsPanel } from '../components/ProjectsPanel.jsx';
import { eventLabel, eventTarget, eventTone, eventWhen } from '../lib/activity.js';
import { api } from '../lib/api.js';
import { useAuth } from '../lib/auth.jsx';
import { useCurrentProject } from '../lib/project.jsx';
import { todoRows } from '../lib/todo.js';
import { useAsync } from '../lib/useAsync.js';

function firstName(user) {
  const name = user?.name?.trim() || user?.email || '';
  return name.split(/[\s@]/)[0] || 'there';
}

function percent(rate) {
  return `${Math.round((rate ?? 0) * 100)}%`;
}

function ActivityRow({ event }) {
  const to = eventTarget(event);
  const body = (
    <>
      <span className={`badge ${eventTone(event.type)}`}>{eventLabel(event.type)}</span>
      <span className="activity-when muted small">{eventWhen(event.created_at)}</span>
    </>
  );
  if (!to) {
    return (
      <div className="activity-row" data-testid="activity-row">
        {body}
      </div>
    );
  }
  return (
    <Link className="activity-row is-link" to={to} data-testid="activity-row">
      {body}
    </Link>
  );
}

function TodoList({ todo }) {
  if (todo.loading) return <Loading label="Checking what's waiting…" />;
  const rows = todoRows(todo.data?.items);
  if (!rows.length) {
    return (
      <div className="muted small" data-testid="todo-clear">
        Nothing is waiting on you — no unanswered replies, no packages stuck, no intake to
        review.
      </div>
    );
  }
  return (
    <div className="stack" style={{ gap: 6 }}>
      {rows.map((row) =>
        row.to ? (
          <Link key={row.key} to={row.to} className="todo-row" data-testid="todo-row">
            <span className={`todo-dot ${row.tone || ''}`} aria-hidden="true" />
            <span className="todo-body">
              <span className="todo-title">{row.title}</span>
              {row.detail ? <span className="muted small">{row.detail}</span> : null}
            </span>
          </Link>
        ) : (
          <div key={row.key} className="todo-row" data-testid="todo-row">
            <span className={`todo-dot ${row.tone || ''}`} aria-hidden="true" />
            <span className="todo-body">
              <span className="todo-title">{row.title}</span>
            </span>
          </div>
        ),
      )}
    </div>
  );
}

export function HomePage() {
  const { user } = useAuth();
  const { project } = useCurrentProject();
  const funnel = useAsync(() => api.funnel(), []);
  const activity = useAsync(() => api.activity(), []);
  const todo = useAsync(() => api.todo(), []);

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
            {/* Shortlisted counts every pipeline card in the org; the closest
                honest destination is the pipeline you are currently in. */}
            <StatLink
              value={shortlisted}
              label="Shortlisted"
              to={project ? `/projects/${project.id}` : '/'}
            />
            <StatLink value={outreach.sent ?? 0} label="Contacted" to="/contacts?view=crm" />
            <StatLink
              value={outreach.replied ?? 0}
              label={`Replied · ${percent(funnel.data?.reply_rate)}`}
              to="/contacts?view=replied"
            />
            {/* Positive-reply intent has no Contacts filter behind it, so this
                one goes to the page that actually breaks it down. */}
            <StatLink
              value={outreach.positive_replies ?? 0}
              label={`Interested · ${percent(funnel.data?.positive_reply_rate)}`}
              to="/analytics"
            />
          </div>
        )}

        <div className="grid two">
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Waiting on you</h3>
            <ErrorBox error={todo.error} />
            <TodoList todo={todo} />
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Recent activity</h3>
            <ErrorBox error={activity.error} />
            {activity.loading ? (
              <Loading label="Loading activity…" />
            ) : activity.data?.length ? (
              <div className="stack" style={{ gap: 2 }}>
                {activity.data.slice(0, 8).map((event) => (
                  <ActivityRow key={event.id} event={event} />
                ))}
              </div>
            ) : (
              <Empty>No activity yet — run a search to get started.</Empty>
            )}
          </div>
        </div>

        <ProjectsPanel />
      </div>
    </>
  );
}
