import { Link, NavLink, Navigate, Route, Routes } from 'react-router-dom';

import { Loading, PageHeader } from './components/common.jsx';
import { useAuth } from './lib/auth.jsx';
import { CurrentProjectProvider, useCurrentProject } from './lib/project.jsx';
import { AgentsPage } from './pages/Agents.jsx';
import { AnalyticsPage } from './pages/Analytics.jsx';
import { CandidateDetailPage } from './pages/CandidateDetail.jsx';
import { ContactsPage } from './pages/Contacts.jsx';
import { HomePage } from './pages/Home.jsx';
import { JobDetailPage } from './pages/JobDetail.jsx';
import { JobsPage } from './pages/Jobs.jsx';
import { LoginPage } from './pages/Login.jsx';
import { ProjectDetailPage, ProjectsPage } from './pages/Projects.jsx';
import { SearchPage } from './pages/Search.jsx';
import { SequenceEditorPage } from './pages/SequenceEditor.jsx';
import { SourcingPage } from './pages/Sourcing.jsx';
import { SequencesPage } from './pages/Sequences.jsx';
import { SettingsPage } from './pages/Settings.jsx';

const NAV = [
  { to: '/', label: 'Home', end: true },
  { to: '/search', label: 'Search' },
  { to: '/sourcing', label: 'Sourcing' },
  { to: '/projects', label: 'Projects' },
  { to: '/jobs', label: 'Jobs' },
  { to: '/sequences', label: 'Sequences' },
  { to: '/contacts', label: 'Contacts' },
  { to: '/analytics', label: 'Analytics' },
  { to: '/settings', label: 'Settings' },
];

function CurrentProjectSection() {
  const { projects, project, setProject } = useCurrentProject();

  if (!projects.length) {
    return (
      <div className="sidebar-section">
        <div className="sidebar-heading">Current project</div>
        <NavLink to="/projects" className="nav-link sub">
          Create your first project
        </NavLink>
      </div>
    );
  }

  return (
    <div className="sidebar-section">
      <div className="sidebar-heading">Current project</div>
      <select
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
      {project ? (
        <>
          <NavLink to={`/projects/${project.id}`} className="nav-link sub">
            Shortlist
          </NavLink>
          <NavLink to="/agents" className="nav-link sub">
            Agent
          </NavLink>
          <NavLink to="/search" className="nav-link sub">
            Searches
          </NavLink>
        </>
      ) : null}
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
        <div className="brand">
          <span className="brand-glyph">▞</span> Foray
        </div>
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
        <CurrentProjectSection />
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
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:postingId" element={<JobDetailPage />} />
          <Route path="/rematch" element={<Navigate to="/jobs?view=rematch" replace />} />
          <Route path="/sequences" element={<SequencesPage />} />
          <Route path="/sequences/:sequenceId" element={<SequenceEditorPage />} />
          <Route path="/outreach" element={<Navigate to="/sequences" replace />} />
          <Route path="/contacts" element={<ContactsPage />} />
          <Route path="/contacts/:candidateId" element={<CandidateDetailPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </Shell>
    </CurrentProjectProvider>
  );
}
