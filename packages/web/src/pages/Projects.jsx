/**
 * A project's pipeline board.
 *
 * The roster that used to live here (create / list / delete) moved to Home:
 * a project is context you work inside, not a section you visit, so picking
 * one belongs in the sidebar and managing them belongs next to everything
 * else on the dashboard.
 */

import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { Empty, ErrorBox, Loading, PageHeader } from '../components/common.jsx';
import { PIPELINE_STAGES, api } from '../lib/api.js';
import { useCurrentProject } from '../lib/project.jsx';
import { useAsync } from '../lib/useAsync.js';

// "contacted" and "replied" are driven by outreach (a send / a reply), not by
// hand — the backend refuses a bare manual move into them. A recruiter who
// reached out themselves can still record it via an explicit override.
const OUTREACH_DRIVEN_STAGES = new Set(['contacted', 'replied']);

export function ProjectDetailPage() {
  const { projectId } = useParams();
  const { projects, project, setProject } = useCurrentProject();
  const pipeline = useAsync(() => api.getPipeline(projectId), [projectId]);
  const funnel = useAsync(() => api.funnel(projectId), [projectId]);
  const [error, setError] = useState(null);

  // Opening a board *is* switching project. Reaching this page from Home's
  // roster used to leave the sidebar naming a different one, and now that the
  // picker has a Pipeline link under it that mismatch is a straight
  // contradiction: the switcher would say one project while the board showed
  // another. Guarded on the id being real so a stale link cannot select a
  // project that is not in the list.
  const known = projects.some((p) => p.id === projectId);
  useEffect(() => {
    if (known && projectId !== project?.id) setProject(projectId);
  }, [known, projectId, project?.id, setProject]);

  async function move(entry, stage, { override = false } = {}) {
    setError(null);
    try {
      await api.updateStage(projectId, entry.id, { stage, override });
      pipeline.reload();
      funnel.reload();
    } catch (caught) {
      setError(caught);
    }
  }

  function manualTouch(entry, stage) {
    const label = stage === 'contacted' ? 'contacted' : 'replied';
    const verb = stage === 'contacted' ? 'reached out to' : 'heard back from';
    if (
      window.confirm(
        `Mark ${entry.candidate.full_name} as ${label}? Only do this if you ${verb} them ` +
          `yourself outside a sequence — otherwise it happens automatically.`,
      )
    ) {
      move(entry, stage, { override: true });
    }
  }

  const byStage = PIPELINE_STAGES.map((stage) => ({
    stage,
    entries: (pipeline.data ?? []).filter((entry) => entry.stage === stage),
  }));

  return (
    <div className="stack">
      <PageHeader
        title="Pipeline"
        // Named from the list rather than from `project`, which trails the URL
        // by one render while the effect above catches up.
        subtitle={`${
          projects.find((p) => p.id === projectId)?.name ?? 'This project'
        } · every card keeps the match evidence that put the person here.`}
        actions={<Link className="badge" to="/">Back to home</Link>}
      />
      <div className="card small muted">
        Sourced → Contacted → Replied advance automatically as outreach sends and replies land.
        Screening onward you move by hand. Use the small "I reached out" link only when you
        contacted someone yourself outside a sequence.
      </div>
      <ErrorBox error={error} />

      {pipeline.loading ? <Loading /> : null}

      {!pipeline.loading && !(pipeline.data ?? []).length ? (
        <Empty>
          Nothing in this pipeline yet — run a search and add people to it.
        </Empty>
      ) : (
        <div className="board">
          {byStage.map(({ stage, entries }) => (
            <div className="column" key={stage}>
              <h4>
                {stage} ({entries.length})
              </h4>
              {entries.map((entry) => {
                const stageIndex = PIPELINE_STAGES.indexOf(entry.stage);
                const next = PIPELINE_STAGES[stageIndex + 1];
                const topCriteria = (entry.rationale?.criteria ?? [])
                  .filter((criterion) => criterion.status === 'strong')
                  .slice(0, 2);
                return (
                  <div className="pipeline-card" key={entry.id}>
                    <div className="name">
                      <Link
                        to={`/contacts/${entry.candidate.id}`}
                        style={{ textDecoration: 'none' }}
                      >
                        {entry.candidate.full_name}
                      </Link>
                    </div>
                    <div className="small muted">
                      {entry.candidate.current_title || '—'}
                      {entry.candidate.current_company ? ` · ${entry.candidate.current_company}` : ''}
                    </div>
                    {entry.score ? (
                      <div className="small muted mono">match {(entry.score * 100).toFixed(0)}</div>
                    ) : null}
                    {topCriteria.map((criterion) => (
                      <div className="small muted" key={criterion.name}>
                        ✓ {criterion.label}
                      </div>
                    ))}
                    <div className="row" style={{ marginTop: 8 }}>
                      {next && !OUTREACH_DRIVEN_STAGES.has(next) ? (
                        <button type="button" className="tiny" onClick={() => move(entry, next)}>
                          → {next}
                        </button>
                      ) : null}
                      {next && OUTREACH_DRIVEN_STAGES.has(next) ? (
                        <button
                          type="button"
                          className="tiny ghost"
                          title={`Records a manual touch — ${next} normally happens automatically`}
                          onClick={() => manualTouch(entry, next)}
                        >
                          {next === 'contacted' ? 'I reached out →' : 'Log reply →'}
                        </button>
                      ) : null}
                      {entry.stage !== 'rejected' ? (
                        <button
                          type="button"
                          className="tiny ghost"
                          onClick={() => move(entry, 'rejected')}
                        >
                          reject
                        </button>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}

      {funnel.data ? (
        <div className="grid four">
          <div className="card stat">
            <div className="value">{funnel.data.outreach.sent}</div>
            <div className="label">emails sent</div>
          </div>
          <div className="card stat">
            <div className="value">{(funnel.data.open_rate * 100).toFixed(0)}%</div>
            <div className="label">open rate</div>
          </div>
          <div className="card stat">
            <div className="value">{(funnel.data.reply_rate * 100).toFixed(0)}%</div>
            <div className="label">reply rate</div>
          </div>
          <div className="card stat">
            <div className="value">{(funnel.data.positive_reply_rate * 100).toFixed(0)}%</div>
            <div className="label">positive replies</div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
