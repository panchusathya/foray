/**
 * Navigation, in two shapes for one route list.
 *
 * A phone gets a five-item tab bar; a desktop gets a rail with the same
 * destinations plus Documents and the signed-in identity. Documents is the
 * one item the tab bar drops — six thumb targets across a phone is a row of
 * things nobody hits, and on a phone Documents is reachable from Profile,
 * which is where people look for their own files anyway.
 */

import { NavLink } from 'react-router-dom';

import { initials } from '../lib/applications.js';
import {
  IconDoc,
  IconHome,
  IconPeople,
  IconSpark,
  IconTracker,
  IconUser,
} from './icons.jsx';

const TABS = [
  { to: '/', label: 'Home', Icon: IconHome, end: true },
  { to: '/discover', label: 'Discover', Icon: IconSpark },
  { to: '/applications', label: 'Tracker', Icon: IconTracker, badge: true },
  { to: '/network', label: 'People', Icon: IconPeople },
  { to: '/profile', label: 'You', Icon: IconUser },
];

const RAIL = [
  { to: '/', label: 'Home', Icon: IconHome, end: true },
  { to: '/discover', label: 'Discover', Icon: IconSpark },
  { to: '/applications', label: 'Applications', Icon: IconTracker, badge: true },
  { to: '/network', label: 'People', Icon: IconPeople },
  { to: '/documents', label: 'Documents', Icon: IconDoc },
];

function Wordmark() {
  return (
    <span className="lockup">
      <span className="glyph" aria-hidden="true">
        <i />
        <i />
        <i />
        <i />
      </span>
      Foray
    </span>
  );
}

export function TabBar({ badge = 0 }) {
  return (
    <nav className="tabbar" aria-label="Primary">
      {TABS.map(({ to, label, Icon, end, badge: wantsBadge }) => (
        <NavLink key={to} to={to} end={end} className={({ isActive }) => (isActive ? 'on' : '')}>
          <Icon />
          <span>{label}</span>
          {wantsBadge && badge > 0 ? (
            <span className="badge" aria-label={`${badge} need you`}>
              {badge > 9 ? '9+' : badge}
            </span>
          ) : null}
        </NavLink>
      ))}
    </nav>
  );
}

export function Rail({ badge = 0, profile }) {
  const candidate = profile?.candidate || {};
  const name = candidate.full_name || `${candidate.first_name || ''} ${candidate.last_name || ''}`.trim();
  const email = (candidate.emails || [])[0] || '';

  return (
    <nav className="rail" aria-label="Primary">
      <Wordmark />
      {RAIL.map(({ to, label, Icon, end, badge: wantsBadge }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          className={({ isActive }) => `nav-link${isActive ? ' on' : ''}`}
        >
          <Icon />
          {label}
          {wantsBadge && badge > 0 ? <span className="badge">{badge}</span> : null}
        </NavLink>
      ))}

      <div className="rail-foot">
        <NavLink to="/profile" className="me">
          <span className="mono sm round">{initials(name || email || '?')}</span>
          <span className="grow truncate">
            <span className="nm truncate" style={{ display: 'block' }}>
              {name || 'Your profile'}
            </span>
            {email ? <span className="em truncate" style={{ display: 'block' }}>{email}</span> : null}
          </span>
        </NavLink>
      </div>
    </nav>
  );
}
