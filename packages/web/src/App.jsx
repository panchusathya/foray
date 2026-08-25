import { Link, NavLink, Navigate, Route, Routes } from 'react-router-dom';

import { BrandMark, Loading, PageHeader } from './components/common.jsx';
import { useAuth } from './lib/auth.jsx';
import { CurrentProjectProvider, useCurrentProject } from './lib/project.jsx';
import { AgentsPage } from './pages/Agents.jsx';
import { AnalyticsPage } from './pages/Analytics.jsx';
import { ApplicationDetailPage, ApplicationsPage } from './pages/Applications.jsx';
import { CandidateDetailPage } from './pages/CandidateDetail.jsx';
import { ContactsPage } from './pages/Contacts.jsx';
import { HomePage } from './pages/Home.jsx';
import { JobDetailPage } from './pages/JobDetail.jsx';
import { JobsPage } from './pages/Jobs.jsx';
import { ImportPage } from './pages/Import.jsx';
import { LoginPage } from './pages/Login.jsx';
import { ProjectDetailPage } from './pages/Projects.jsx';
import { SearchPage } from './pages/Search.jsx';
import { SequenceEditorPage } from './pages/SequenceEditor.jsx';
import { SourcingPage } from './pages/Sourcing.jsx';
import { SequencesPage } from './pages/Sequences.jsx';
import { SettingsPage } from './pages/Settings.jsx';
import { IntakePage } from './pages/Intake.jsx';

const NAV = [
  { to: '/', label: 'Home', end: true },
  { to: '/search', label: 'Search' },
  { to: '/sourcing', label: 'Sourcing' },
  { to: '/jobs', label: 'Jobs' },
  { to: '/applications', label: 'Applications' },
  { to: '/sequences', label: 'Sequences' },
  { to: '/import', label: 'Import' },
  { to: '/contacts', label: 'Contacts' },
  { to: '/intake', label: 'Intake' },
  { to: '/analytics', label: 'Analytics' },
  { to: '/settings', label: 'Settings' },
];

/**
 * The project picker, at the top of the sidebar where the thing you are
 * working *in* belongs.
 *
 * It used to sit below eleven nav links as a heading, a dropdown and three
 * sub-links, which read as a fourth navigation section rather than as context.
 * It is now just the picker: the shortcuts it carried are one click away on
 * the project's own page, and the roster it needed a "Projects" nav item for
 * lives on Home.
 */
function ProjectPicker() {
  const { projects, project, setProject } = useCurrentProject();

  return (
    <div className="project-picker">
      <label className="sidebar-heading" htmlFor="project-switcher">
        Project
      </label>
      {projects.length ? (
        <select
          id="project-switcher"
          className="project-switcher"
          value={project?.id ?? ''}
          onChange={(event) => setProject(event.target.value)}
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      ) : (
        <NavLink to="/" className="nav-link sub">
          Create your first project
        </NavLink>
      )}
    </div>
  );
}

function NotFoundPage() {
  return (
    <div className="stack">
      <PageHeader title="Page not found" subtitle="That page doesn't exist or has moved." />
      <div className="card empty">
        <Link to="/" className="nav-link">
          Back to home
        </Link>
      </div>
    </div>
  );
}

function Shell({ children }) {
  const { user, logout } = useAuth();
  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="brand brand-lockup">
          <BrandMark />
          <span>Foray</span>
        </div>
        <ProjectPicker />
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
          >
            {item.label}
          </NavLink>
        ))}
        <div className="sidebar-footer">
          <div>{user?.name || user?.email}</div>
          <button type="button" className="tiny ghost" onClick={logout} style={{ marginTop: 8 }}>
            Log out
          </button>
        </div>
      </nav>
      <main className="main">{children}</main>
    </div>
  );
}

export function App() {
  const { user, loading } = useAuth();

  if (loading) return <Loading label="Checking your session…" />;
  if (!user) return <LoginPage />;

  return (
    <CurrentProjectProvider>
      <Shell>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/sourcing" element={<SourcingPage />} />
          <Route path="/projects" element={<Navigate to="/" replace />} />
          <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:postingId" element={<JobDetailPage />} />
          <Route path="/rematch" element={<Navigate to="/jobs?view=rematch" replace />} />
          <Route path="/applications" element={<ApplicationsPage />} />
          <Route path="/applications/:applicationId" element={<ApplicationDetailPage />} />
          <Route path="/import" element={<ImportPage />} />
          <Route path="/sequences" element={<SequencesPage />} />
          <Route path="/sequences/:sequenceId" element={<SequenceEditorPage />} />
          <Route path="/outreach" element={<Navigate to="/sequences" replace />} />
          <Route path="/contacts" element={<ContactsPage />} />
          <Route path="/contacts/:candidateId" element={<CandidateDetailPage />} />
          <Route path="/intake" element={<IntakePage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </Shell>
    </CurrentProjectProvider>
  );
}
