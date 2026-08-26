/**
 * Home — the answer to "where does my job search actually stand".
 *
 * Ordered by who is blocked, not by what is newest: anything waiting on the
 * candidate comes first, then the numbers, then movement, then the people
 * worth messaging. Everything on this screen is a link into the tab that owns
 * it — home summarises and never becomes a fourth place to do the work.
 */

import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { IconArrow, IconBuilding, IconWrite } from '../components/icons.jsx';
import {
  CopyButton,
  Empty,
  ErrorNote,
  Mono,
  Screen,
  SectionHead,
  Skeleton,
  Stat,
  StatusChip,
} from '../components/ui.jsx';
import { api } from '../lib/api.js';
import { actionPrompt, ago, byRecency, greeting, isClosed, summarise } from '../lib/applications.js';
import { useAuth } from '../lib/auth.jsx';
import { mapLimit, mergePeople } from '../lib/network.js';
import { useApplications } from '../lib/store.jsx';

// How many open applications to pull contacts for on the home screen. The
// People tab loads the rest; this is a teaser, and each one is a request.
const PEEK_APPLICATIONS = 4;
const PEEK_PEOPLE = 6;

export function HomePage() {
  const { profile } = useAuth();
  const { applications, loading, error } = useApplications();
  const rows = applications || [];
  const stats = summarise(rows);
  const recent = byRecency(rows);
  const todo = recent.filter((row) => actionPrompt(row));
  const first = profile?.candidate?.first_name || '';

  return (
    <Screen>
      <section className="hello">
        <h1>
          {greeting()}
          {first ? `, ${first}` : ''}.
        </h1>
        <p>{headline(stats, loading)}</p>
        {!loading && stats.total === 0 ? (
          <Link className="btn primary" to="/discover">
            Find roles worth applying to
            <IconArrow />
          </Link>
        ) : null}
      </section>

      <ErrorNote error={error} />

      <div className="stat-row">
        <Stat n={stats.live} k="In flight" to="/applications" />
        <Stat n={stats.needsYou} k="Need you" tone={stats.needsYou ? 'gold' : ''} to="/applications" />
        <Stat n={stats.interviewing} k="Interviewing" tone="accent" to="/applications" />
        <Stat n={stats.offers} k="Offers" tone="accent" to="/applications" />
      </div>

      {loading ? <Skeleton count={3} height={72} /> : null}

      {todo.length > 0 ? (
        <section className="section">
          <SectionHead label="Waiting on you" to="/applications" />
          <div className="stack tight">
            {todo.slice(0, 4).map((application) => (
              <Link key={application.id} to={`/applications/${application.id}`} className="todo">
                <Mono name={application.company} tone="gold" />
                <span className="grow">
                  <span className="strong truncate" style={{ display: 'block' }}>
                    {application.title || 'Role'}
                  </span>
                  <span className="why">{actionPrompt(application)}</span>
                </span>
                <IconArrow style={{ width: 17, height: 17, color: 'var(--faint)', flex: '0 0 auto' }} />
              </Link>
            ))}
          </div>
        </section>
      ) : null}

      {!loading && stats.total > 0 ? (
        <section className="section">
          <SectionHead label="Latest movement" to="/applications" />
          <div className="stack tight">
            {recent.slice(0, 4).map((application) => (
              <Link
                key={application.id}
                to={`/applications/${application.id}`}
                className="app-card"
              >
                <Mono name={application.company} />
                <span className="grow">
                  <span className="title truncate" style={{ display: 'block' }}>
                    {application.title || 'Role'}
                  </span>
                  <span className="meta truncate" style={{ display: 'block' }}>
                    {application.company}
                  </span>
                </span>
                <span className="stack tight" style={{ alignItems: 'flex-end', gap: 4 }}>
                  <StatusChip status={application.status} />
                  <span className="when">{ago(application.updated_at)}</span>
                </span>
              </Link>
            ))}
          </div>
        </section>
      ) : null}

      {!loading && stats.total === 0 ? (
        <Empty title="Nothing in flight yet" glyph="→">
          Swipe through a few roles and we will write the application for you — resume, cover
          letter, and the answers to their form.
        </Empty>
      ) : null}

      <PeopleTeaser applications={rows} />
    </Screen>
  );
}

function headline(stats, loading) {
  if (loading) return 'Pulling your applications together…';
  if (stats.total === 0) return 'Your applications, your network and every draft we write, in one place.';
  if (stats.needsYou > 0) {
    return `${stats.needsYou} ${stats.needsYou === 1 ? 'application is' : 'applications are'} ready for your review — everything else is moving on its own.`;
  }
  if (stats.offers > 0) return 'You have an offer on the table. Nice.';
  if (stats.interviewing > 0) return "You're interviewing. We'll keep the rest moving.";
  return `${stats.live} ${stats.live === 1 ? 'application' : 'applications'} in flight. Nothing needs you right now.`;
}

/**
 * A peek at the People tab: contacts at the places you have applied, with the
 * message we already wrote. Loads quietly and renders nothing until it has
 * somebody — an empty rail on the home screen is worse than no rail.
 */
function PeopleTeaser({ applications }) {
  const [people, setPeople] = useState([]);

  const open = applications.filter((row) => !isClosed(row.status)).slice(0, PEEK_APPLICATIONS);
  // Keyed on which applications, not on the array's identity: the store hands
  // back a new array on every refresh, and refetching contacts each time a
  // status changed elsewhere is a request per application for no new answer.
  const key = open.map((row) => row.id).join(',');
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => {
    const rows = openRef.current;
    if (rows.length === 0) {
      setPeople([]);
      return undefined;
    }
    let cancelled = false;
    mapLimit(rows, 3, async (application) => ({
      application,
      suggestions: (await api.networking(application.id)).suggestions || [],
    })).then((entries) => {
      if (!cancelled) setPeople(mergePeople(entries.filter(Boolean)).slice(0, PEEK_PEOPLE));
    });
    return () => {
      cancelled = true;
    };
  }, [key]);

  if (people.length === 0) return null;

  return (
    <section className="section">
      <SectionHead label="Worth messaging" to="/network" />
      <div className="rail-scroll">
        {people.map((person) => (
          <article key={person.id} className="person">
            <div className="who">
              <Mono name={person.name} tone="" size="" />
              <div className="grow">
                <div className="name truncate">{person.name}</div>
                <div className="role truncate">{person.title || '—'}</div>
                <div className="at">
                  <IconBuilding />
                  {person.at[0]?.company || person.company}
                </div>
              </div>
            </div>
            {person.draft ? (
              <CopyButton text={person.draft} label="Copy message" className="btn small wide" />
            ) : (
              <Link to="/network" className="btn small wide">
                <IconWrite />
                Draft a message
              </Link>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
