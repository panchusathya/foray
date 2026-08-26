/**
 * Discover — the swipe deck.
 *
 * One decision per screen, and the decision is "should we apply for you".
 * Everything that is not that decision (search, the result of the last swipe)
 * sits above the deck and stays out of the drag surface.
 */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { CardDeck } from '../components/CardDeck.jsx';
import { IconArrow, IconSearch } from '../components/icons.jsx';
import { Empty, ErrorNote, Head, Note, Screen } from '../components/ui.jsx';
import { api } from '../lib/api.js';
import { useApplications } from '../lib/store.jsx';

export function DiscoverPage() {
  const { reload } = useApplications();
  const [cards, setCards] = useState(null);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState('');
  const [note, setNote] = useState('');

  const load = useCallback(async (q = '') => {
    setError(null);
    try {
      const data = await api.feed({ q });
      setCards(data.cards || []);
    } catch (err) {
      setError(err);
      setCards([]);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onSwipe = useCallback(
    async (card, direction) => {
      const result = await api.swipe(card.posting_id, direction);
      if (result.status === 'parked') {
        // Told, not hidden: a swipe that did not become an application
        // because of a weekly cap has to say so, or the deck silently eats
        // somebody's intent.
        setNote(
          `Noted ${card.title} at ${card.company} — you're at this week's application limit, so it's queued.`,
        );
      } else if (result.status === 'accepted') {
        setNote(`On it — writing your application for ${card.title} at ${card.company}.`);
        reload();
      }
      return result;
    },
    [reload],
  );

  const onSearch = (event) => {
    event.preventDefault();
    setCards(null);
    load(query);
  };

  return (
    <Screen>
      <Head title="Discover" sub="Right to apply, left to skip. We write the rest." />

      <form onSubmit={onSearch}>
        <label className="search">
          <IconSearch />
          <input
            type="search"
            value={query}
            placeholder="Filter by title or company"
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Search roles"
          />
        </label>
      </form>

      <div className="stack tight" style={{ marginTop: 'var(--s-3)' }}>
        <ErrorNote error={error} />
        {note ? <Note tone="good">{note}</Note> : null}
      </div>

      {cards === null ? (
        <div className="skel" style={{ height: 420, marginTop: 'var(--s-3)' }} />
      ) : null}

      {cards !== null && cards.length === 0 ? (
        <div style={{ marginTop: 'var(--s-4)' }}>
          <Empty
            title="Nothing new right now"
            glyph="~"
            action={
              <Link className="btn" to="/applications">
                See your applications
                <IconArrow />
              </Link>
            }
          >
            Your agent is still looking. It will message you the moment something clears the bar
            you set.
          </Empty>
        </div>
      ) : null}

      {cards !== null && cards.length > 0 ? (
        <CardDeck
          cards={cards}
          onSwipe={onSwipe}
          onExhausted={() => (
            <Empty
              title="That's everything for now"
              glyph="✓"
              action={
                <Link className="btn primary" to="/applications">
                  See your applications
                  <IconArrow />
                </Link>
              }
            >
              New roles land here as they are found.
            </Empty>
          )}
        />
      ) : null}
    </Screen>
  );
}
