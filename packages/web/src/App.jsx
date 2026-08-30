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

/**
 * The nav, grouped by the question each section answers.
 *
 * Flat, it was ten links in no order, above which a project picker appeared to
 * govern everything. It governed three things. Worse, the list interleaved two
 * different jobs: filling a role for a client (a project, scoped) and placing a
 * person we represent (Intake -> Jobs -> Applications, never scoped to a
 * project -- see the note at the top of Applications.jsx). Neither read as a
 * sequence of steps because each was every other step apart.
 *
 * Project links live with the picker rather than in this table: "Pipeline"
 * needs the selected project's id, so it cannot be a constant.
 */
const NAV_SECTIONS = [
  {
    heading: 'Find people',
    items: [
      { to: '/search', label: 'Search' },
      { to: '/sourcing', label: 'Sourcing' },
      { to: '/import', label: 'Import' },
    ],
  },
  {
    heading: 'Place people',
    items: [
      { to: '/intake', label: 'Intake' },
      { to: '/jobs', label: 'Jobs' },
      { to: '/applications', label: 'Applications' },
    ],
  },
];

function NavItem({ to, label, end }) {
  return (
    <NavLink to={to} end={end} className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
      {label}
    </NavLink>
  );
}

/**
 * The project you are working in, and the three pages that run inside it.
 *
 * The links under the picker were removed once, on the grounds that a lone
 * grouped block read as a fourth navigation section rather than as context.
 * That was true of one island among ten ungrouped links; a section among
 * sections is not an anomaly. Two things have also changed since: Contacts is
 * genuinely project-scoped now, so the picker has reach that a flat list hid
 * entirely, and what was removed was a roster of *all* projects, where
 * "Pipeline" is the board for the one you have selected.
 */
function ProjectSection() {
  const { projects, project, setProject } = useCurrentProject();

  return (
    <div className="sidebar-section">
      {/* A <label> only when there is a control for it to name; with no
          projects the switcher is not rendered and the htmlFor would dangle. */}
      {projects.length ? (
        <>
          <label className="sidebar-heading" htmlFor="project-switcher">
            Project
          </label>
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
        </>
      ) : (
        <div className="sidebar-heading">Project</div>
      )}

      {/* Pipeline is the one link with nowhere to go before there is a project,
          so it is the one that gives up its slot. Contacts and Sequences are
          both fine unscoped and stay put. */}
      {project ? (
        <NavItem to={`/projects/${project.id}`} label="Pipeline" />
      ) : (
        <NavLink to="/" className="nav-link sub">
          Create your first project
        </NavLink>
      )}
      <NavItem to="/contacts" label="Contacts" />
      <NavItem to="/sequences" label="Sequences" />
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
      {/* One <nav>, and the sections are plain divs. ViewTabs is already a
          second navigation landmark, and auth.spec.js reaches the sidebar as
          `getByRole('navigation').…first()` -- more landmarks here would hand
          that `.first()` the wrong element. */}
      <nav className="sidebar">
        <div className="brand brand-lockup">
          <BrandMark />
          <span>Foray</span>
        </div>
        <NavItem to="/" label="Home" end />
        <ProjectSection />
        {NAV_SECTIONS.map((section) => (
          <div className="sidebar-section" key={section.heading}>
            <div className="sidebar-heading">{section.heading}</div>
            {section.items.map((item) => (
              <NavItem key={item.to} {...item} />
            ))}
          </div>
        ))}
        <div className="sidebar-footer">
          <div>{user?.name || user?.email}</div>
          {/* Account furniture, not a step in either flow. */}
          <div className="sidebar-account">
            <NavLink
              to="/settings"
              className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            >
              Settings
            </NavLink>
            <button type="button" className="tiny ghost" onClick={logout}>
              Log out
            </button>
          </div>
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
          {/* Off the sidebar -- nobody was reading it as a destination. Still
              routed: Home's "Interested" stat links here, and that breakdown
              has nowhere else to live. */}
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </Shell>
    </CurrentProjectProvider>
  );
}
