import { NavLink, Navigate, Route, Routes } from 'react-router-dom';

import { Loading } from './components/common.jsx';
import { useAuth } from './lib/auth.jsx';
import { AgentsPage } from './pages/Agents.jsx';
import { AnalyticsPage } from './pages/Analytics.jsx';
import { LoginPage } from './pages/Login.jsx';
import { OutreachPage } from './pages/Outreach.jsx';
import { ProjectDetailPage, ProjectsPage } from './pages/Projects.jsx';
import { SearchPage } from './pages/Search.jsx';

const NAV = [
  { to: '/', label: 'Search', end: true },
  { to: '/projects', label: 'Projects' },
  { to: '/outreach', label: 'Outreach' },
  { to: '/agents', label: 'Agents' },
  { to: '/analytics', label: 'Analytics' },
];

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
    <Shell>
      <Routes>
        <Route path="/" element={<SearchPage />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
        <Route path="/outreach" element={<OutreachPage />} />
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}
