import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { CardDeck } from '../components/CardDeck.jsx';
import { Empty, ErrorNote, Screen, Skeleton } from '../components/ui.jsx';
import { api } from '../lib/api.js';

export function DiscoverPage() {
  const [cards, setCards] = useState(null);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState('');
  const [note, setNote] = useState('');
  const navigate = useNavigate();

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

  const onSwipe = useCallback(async (card, direction) => {
    const result = await api.swipe(card.posting_id, direction);
    if (result.status === 'parked') {
      // Told, not hidden: a swipe that did not become an application because
      // of a weekly cap has to say so, or the deck silently eats intent.
      setNote(
        `Noted ${card.title} at ${card.company} — you're at this week's application limit, so it's queued.`,
      );
    } else if (result.status === 'accepted') {
      setNote(`Preparing your application for ${card.title} at ${card.company}.`);
    }
    return result;
  }, []);

  const onSearch = (event) => {
    event.preventDefault();
    setCards(null);
    load(query);
  };

  return (
    <Screen title="Discover" sub="Swipe right to apply, left to skip.">
      <form className="row" onSubmit={onSearch} style={{ marginBottom: 12 }}>
        <input
          className="field"
          style={{ flex: 1, minHeight: 44, padding: '10px 12px', borderRadius: 8, border: '1px solid var(--line)', background: 'var(--surface)', color: 'var(--ink)' }}
          placeholder="Search by title or company"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Search roles"
        />
        <button type="submit" className="btn">
          Search
        </button>
      </form>

      <ErrorNote error={error} />
      {note && <div className="banner info">{note}</div>}

      {cards === null && <Skeleton height={220} count={1} />}
      {cards !== null && cards.length === 0 && (
        <Empty title="Nothing new right now">
          Your recruiter&apos;s agent is still looking. It will text you the moment something
          clears the bar.
        </Empty>
      )}
      {cards !== null && cards.length > 0 && (
        <CardDeck
          cards={cards}
          onSwipe={onSwipe}
          onExhausted={() => (
            <Empty
              title="That's everything for now"
              action={
                <button type="button" className="btn" onClick={() => navigate('/applications')}>
                  See your applications
                </button>
              }
            >
              New roles land here as they are found.
            </Empty>
          )}
        />
      )}
    </Screen>
  );
}
