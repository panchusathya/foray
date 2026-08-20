import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { PageHeader, ViewTabs } from '../components/common.jsx';
import { JOB_VIEWS, viewById } from '../lib/jobViews.js';
import { JobsAdd } from './jobs/JobsAdd.jsx';
import { JobsDiscover } from './jobs/JobsDiscover.jsx';
import { JobsOverview } from './jobs/JobsOverview.jsx';
import { RematchPanel } from './Rematch.jsx';
import './jobs.css';

export function JobsPage() {
  const [searchParams] = useSearchParams();
  const view = viewById(searchParams.get('view'));
  // Discover state lives on the shell so switching to Overview does not
  // wipe the thread or the ticks. Still session-only — no table.
  const [discoverTurns, setDiscoverTurns] = useState([]);
  const [discoverSelected, setDiscoverSelected] = useState(new Map());

  return (
    <div className="stack">
      <PageHeader title="Jobs" subtitle={view.blurb} />
      <ViewTabs views={JOB_VIEWS} activeId={view.id} basePath="/jobs" />
      {view.panel === 'overview' ? <JobsOverview /> : null}
      {view.panel === 'add' ? <JobsAdd /> : null}
      {view.panel === 'discover' ? (
        <JobsDiscover
          turns={discoverTurns}
          setTurns={setDiscoverTurns}
          selected={discoverSelected}
          setSelected={setDiscoverSelected}
        />
      ) : null}
      {view.panel === 'rematch' ? <RematchPanel /> : null}
    </div>
  );
}
