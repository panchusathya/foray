/**
 * People — who the candidate should talk to, and what to say.
 *
 * The API is per-application ("who do we know where this went"), so this page
 * fans out across the applications and folds the answers into one row per
 * human (lib/network.js). That fold is the whole point of the tab: somebody
 * who works at a company you applied to twice is one person to message, not
 * two rows to be confused by.
 *
 * We draft, they send. Every action here ends in the candidate's own hands —
 * copy the message, open the profile — because an introduction sent on
 * somebody's behalf, to somebody who never asked for it, is a different
 * product and a worse one.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { IconBuilding, IconExternal, IconSearch, IconWrite } from '../components/icons.jsx';
import {
  CopyButton,
  Empty,
  ErrorNote,
  Head,
  Mono,
  Screen,
  Sheet,
  Skeleton,
} from '../components/ui.jsx';
import { api } from '../lib/api.js';
import { isClosed } from '../lib/applications.js';
import {
  atCompany,
  basisLabel,
  companiesOf,
  mapLimit,
  matchesQuery,
  mergePeople,
} from '../lib/network.js';
import { useApplications } from '../lib/store.jsx';

// Four at a time: the contacts endpoint computes on demand, and twenty
// applications firing twenty requests at once is a self-inflicted stampede.
const FANOUT = 4;

export function NetworkPage() {
  const { applications, loading: loadingApps } = useApplications();
  const [people, setPeople] = useState(null);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState('');
  const [company, setCompany] = useState('');
  const [open, setOpen] = useState(null);

  // Closed applications are dropped: the reason to message somebody is an
  // application that is still alive.
  const live = useMemo(
    () => (applications || []).filter((row) => !isClosed(row.status)),
    [applications],
  );

  // The store hands back a fresh array whenever it reloads — which it does
  // every time the tab is brought to the front — and depending on it directly
  // meant refetching every contact, and flashing skeletons over a page
  // somebody was reading, because a status changed somewhere else. Refetch
  // when the *set of applications* changes, not when its identity does.
  const liveKey = live.map((row) => row.id).join(',');
  const liveRef = useRef(live);
  liveRef.current = live;

  useEffect(() => {
    if (loadingApps) return undefined;
    const rows = liveRef.current;
    if (rows.length === 0) {
      setPeople([]);
      return undefined;
    }
    let cancelled = false;
    setPeople(null);
    mapLimit(rows, FANOUT, async (application) => ({
      application,
      suggestions: (await api.networking(application.id)).suggestions || [],
    }))
      .then((entries) => {
        if (cancelled) return;
        const usable = entries.filter(Boolean);
        setError(
          usable.length === 0
            ? new Error("We couldn't load your contacts just now. Try again in a moment.")
            : null,
        );
        setPeople(mergePeople(usable));
      })
      .catch((err) => !cancelled && setError(err));
    return () => {
      cancelled = true;
    };
  }, [liveKey, loadingApps]);

  const rows = people || [];
  const companies = companiesOf(rows);
  const shown = rows.filter((person) => atCompany(person, company) && matchesQuery(person, query));
  const drafted = rows.filter((person) => person.draft).length;

  /** Fold a fresh draft back into the row it belongs to. */
  const onDrafted = useCallback((personId, draft) => {
    setPeople((current) =>
      (current || []).map((person) => (person.id === personId ? { ...person, draft } : person)),
    );
    setOpen((current) => (current && current.id === personId ? { ...current, draft } : current));
  }, []);

  return (
    <Screen wide>
      <Head
        title="People"
        sub={
          rows.length
            ? `${rows.length} ${rows.length === 1 ? 'person' : 'people'} where you've applied · ${drafted} ${drafted === 1 ? 'message' : 'messages'} written`
            : 'Who to talk to where you have applied.'
        }
      />

      <ErrorNote error={error} />

      {rows.length > 0 ? (
        <>
          <label className="search">
            <IconSearch />
            <input
              type="search"
              value={query}
              placeholder="Search a name, role or company"
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Search people"
            />
          </label>

          {companies.length > 1 ? (
            <div className="filters" style={{ marginTop: 'var(--s-3)' }}>
              <button
                type="button"
                className={`filter${company === '' ? ' on' : ''}`}
                onClick={() => setCompany('')}
                aria-pressed={company === ''}
              >
                Everyone
                <span className="count">{rows.length}</span>
              </button>
              {companies.map((entry) => (
                <button
                  key={entry.company}
                  type="button"
                  className={`filter${company === entry.company ? ' on' : ''}`}
                  onClick={() => setCompany(entry.company)}
                  aria-pressed={company === entry.company}
                >
                  {entry.company}
                  <span className="count">{entry.count}</span>
                </button>
              ))}
            </div>
          ) : null}
        </>
      ) : null}

      {people === null || loadingApps ? (
        <div style={{ marginTop: 'var(--s-4)' }}>
          <Skeleton count={3} height={132} />
        </div>
      ) : null}

      {people !== null && rows.length === 0 ? (
        <Empty
          title="No one to introduce you yet"
          glyph="?"
          action={
            <Link className="btn primary" to="/discover">
              Find roles
            </Link>
          }
        >
          Apply somewhere first. As soon as an application is live we look for people we know at
          that company and write your opening message.
        </Empty>
      ) : null}

      {shown.length === 0 && rows.length > 0 ? (
        <Empty title="Nobody matches that">Try a different name, or clear the filter.</Empty>
      ) : null}

      <div className="grid-2">
        {shown.map((person) => (
          <PersonCard key={person.id} person={person} onOpen={() => setOpen(person)} />
        ))}
      </div>

      {open ? (
        <MessageSheet person={open} onClose={() => setOpen(null)} onDrafted={onDrafted} />
      ) : null}
    </Screen>
  );
}

function PersonCard({ person, onOpen }) {
  const where = person.at[0];
  return (
    <article className="person">
      <div className="who">
        <Mono name={person.name} />
        <div className="grow">
          <div className="name truncate">{person.name}</div>
          <div className="role truncate">{person.title || '—'}</div>
          <div className="at">
            <IconBuilding />
            {where?.company || person.company}
            {person.at.length > 1 ? ` · ${person.at.length} of your applications` : ''}
          </div>
        </div>
        {person.basis ? <span className="chip">{basisLabel(person.basis)}</span> : null}
      </div>

      {person.draft ? (
        <p className="draft">{person.draft}</p>
      ) : (
        <p className="person-empty">
          No message written yet — we&apos;ll draft one about{' '}
          {where?.title ? `the ${where.title} role` : 'your application'}.
        </p>
      )}

      <div className="btn-row">
        <button type="button" className="btn small soft" onClick={onOpen}>
          <IconWrite />
          {person.draft ? 'Read & copy' : 'Draft a message'}
        </button>
        {person.linkedin_url ? (
          <a className="btn small ghost" href={person.linkedin_url} target="_blank" rel="noreferrer">
            LinkedIn
            <IconExternal />
          </a>
        ) : null}
      </div>
    </article>
  );
}

/**
 * The message, editable before it is copied.
 *
 * Editable because it is the candidate's message in the candidate's name;
 * shipping a draft they cannot touch would mean either sending our words as
 * theirs or retyping the whole thing somewhere else.
 */
function MessageSheet({ person, onClose, onDrafted }) {
  const where = person.at[0];
  const [text, setText] = useState(person.draft || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const write = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const applicationId = person.draftFor || where?.applicationId;
      const { draft } = await api.draftIntro(applicationId, person.id);
      setText(draft || '');
      onDrafted(person.id, draft || '');
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }, [person, where, onDrafted]);

  // Nothing written yet: write it the moment the sheet opens rather than
  // making somebody tap a second button to see why they opened this.
  useEffect(() => {
    if (!person.draft) write();
    // Only on open — a re-draft is an explicit choice, never a re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Sheet
      title={`Message ${person.name}`}
      sub={
        where
          ? `About your ${where.title || 'application'} application at ${where.company}. Send it yourself — we never send these for you.`
          : 'Send it yourself — we never send these for you.'
      }
      onClose={onClose}
    >
      <ErrorNote error={error} />

      {busy && !text ? (
        <div className="skel" style={{ height: 190 }} />
      ) : (
        <textarea
          value={text}
          onChange={(event) => setText(event.target.value)}
          aria-label="Your message"
          spellCheck
        />
      )}

      <div className="btn-row" style={{ marginTop: 'var(--s-3)' }}>
        <CopyButton text={text} label="Copy message" className="btn primary grow" />
        {person.linkedin_url ? (
          <a className="btn" href={person.linkedin_url} target="_blank" rel="noreferrer">
            Open profile
            <IconExternal />
          </a>
        ) : null}
        <button type="button" className="btn ghost" disabled={busy} onClick={write}>
          {busy ? 'Rewriting…' : 'Rewrite'}
        </button>
      </div>
    </Sheet>
  );
}
