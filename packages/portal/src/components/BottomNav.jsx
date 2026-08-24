import { NavLink } from 'react-router-dom';

const TABS = [
  { to: '/', label: 'Discover', end: true },
  { to: '/applications', label: 'Applications' },
  { to: '/documents', label: 'Documents' },
  { to: '/profile', label: 'Profile' },
];

export function BottomNav({ badge = 0 }) {
  return (
    <nav className="nav">
      {TABS.map((tab) => (
        <NavLink key={tab.to} to={tab.to} end={tab.end} className={({ isActive }) => (isActive ? 'on' : '')}>
          <span>{tab.label}</span>
          {tab.to === '/applications' && badge > 0 ? <span className="dot" /> : null}
        </NavLink>
      ))}
    </nav>
  );
}
