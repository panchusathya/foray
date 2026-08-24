import { useSearchParams } from 'react-router-dom';

import { PageHeader, ViewTabs } from '../components/common.jsx';
import { INTAKE_VIEWS, viewById } from '../lib/intakeViews.js';
import { IntakeCalls } from './intake/IntakeCalls.jsx';
import { IntakeMatches } from './intake/IntakeMatches.jsx';
import { IntakeReview } from './intake/IntakeReview.jsx';
import { IntakeThreads } from './intake/IntakeThreads.jsx';
import './jobs.css';

export function IntakePage() {
  const [searchParams] = useSearchParams();
  const view = viewById(searchParams.get('view'));

  return (
    <div className="stack">
      <PageHeader title="Intake" subtitle={view.blurb} />
      <ViewTabs views={INTAKE_VIEWS} activeId={view.id} basePath="/intake" />
      {view.panel === 'calls' ? <IntakeCalls /> : null}
      {view.panel === 'text' ? <IntakeThreads /> : null}
      {view.panel === 'review' ? <IntakeReview /> : null}
      {view.panel === 'matches' ? <IntakeMatches /> : null}
    </div>
  );
}
