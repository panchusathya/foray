import { useCallback, useEffect, useReducer, useRef, useState } from 'react';

import {
  SWIPE_LEFT,
  SWIPE_RIGHT,
  currentCard,
  deckReducer,
  directionFor,
  initialDeck,
  isExhausted,
  upcoming,
} from '../lib/deck.js';
import { IconCheck, IconX } from './icons.jsx';

// Pointer events, not touch events: one code path covers finger, trackpad and
// mouse, so a desktop gets the same deck instead of a degraded list. The
// reducer in lib/deck.js owns every outcome; this component owns the transform
// and nothing else.

export function CardDeck({ cards, onSwipe, onExhausted }) {
  const [state, dispatch] = useReducer(deckReducer, cards, initialDeck);
  const [drag, setDrag] = useState({ x: 0, active: false });
  const start = useRef(0);
  const pointer = useRef(null);

  const card = currentCard(state);
  const next = upcoming(state, 2);

  const commit = useCallback(
    (direction) => {
      const answered = currentCard(state);
      if (!answered) return;
      dispatch({ type: 'swipe', direction });
      setDrag({ x: 0, active: false });
      Promise.resolve(onSwipe(answered, direction))
        .then((result) =>
          dispatch({
            type: 'settled',
            postingId: answered.posting_id,
            status: (result && result.status) || 'accepted',
          }),
        )
        .catch(() => dispatch({ type: 'failed', postingId: answered.posting_id }));
    },
    [onSwipe, state],
  );

  // Arrow keys drive the deck too. Somebody triaging fifty roles on a laptop
  // should never have to move their hand to the trackpad.
  useEffect(() => {
    const onKey = (event) => {
      // `closest` only exists on elements — a keydown with no focused element
      // reports document as its target on some browsers.
      if (event.target?.closest?.('input, textarea')) return;
      if (event.key === 'ArrowRight') commit(SWIPE_RIGHT);
      if (event.key === 'ArrowLeft') commit(SWIPE_LEFT);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [commit]);

  const onPointerDown = (event) => {
    if (!card) return;
    pointer.current = event.pointerId;
    start.current = event.clientX;
    event.currentTarget.setPointerCapture(event.pointerId);
    setDrag({ x: 0, active: true });
  };

  const onPointerMove = (event) => {
    if (pointer.current !== event.pointerId || !drag.active) return;
    setDrag({ x: event.clientX - start.current, active: true });
  };

  const onPointerUp = (event) => {
    if (pointer.current !== event.pointerId) return;
    pointer.current = null;
    const direction = directionFor(drag.x);
    if (direction) commit(direction);
    else setDrag({ x: 0, active: false });
  };

  if (!card) {
    if (isExhausted(state) && onExhausted) return onExhausted(state);
    return null;
  }

  const rotation = drag.x / 22;
  const transform = `translate3d(${drag.x}px, 0, 0) rotate(${rotation}deg)`;
  const applyOpacity = Math.max(0, Math.min(1, drag.x / 90));
  const skipOpacity = Math.max(0, Math.min(1, -drag.x / 90));

  return (
    <>
      <div className="deck">
        {next
          .slice()
          .reverse()
          .map((behind, index) => (
            <article
              key={behind.posting_id}
              className="deck-card behind"
              aria-hidden="true"
              style={{
                transform: `translate3d(0, ${(next.length - index) * 9}px, 0) scale(${
                  1 - (next.length - index) * 0.03
                })`,
              }}
            >
              <div className="company">{behind.company}</div>
              <h2>{behind.title}</h2>
            </article>
          ))}

        <article
          className="deck-card"
          style={{
            transform,
            transition: drag.active ? 'none' : 'transform 220ms cubic-bezier(.2,.8,.3,1)',
          }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          <span className="verdict apply" style={{ opacity: applyOpacity }}>
            APPLY
          </span>
          <span className="verdict skip" style={{ opacity: skipOpacity }}>
            SKIP
          </span>

          <div className="company">{card.company}</div>
          <h2>{card.title}</h2>
          <div className="chips">
            {card.location ? <span className="chip">{card.location}</span> : null}
            {card.seniority ? <span className="chip">{card.seniority}</span> : null}
            {card.source_label ? <span className="chip accent">{card.source_label}</span> : null}
          </div>
          <div className="reasons">
            {(card.reasons || []).map((reason) => (
              <div key={reason} className="reason">
                <IconCheck />
                <span>{reason}</span>
              </div>
            ))}
          </div>
        </article>
      </div>

      {/* The buttons are not a fallback: they are how this works with a
          keyboard, a screen reader, or a thumb that would rather tap. */}
      <div className="deck-actions">
        <button type="button" className="swipe skip" onClick={() => commit(SWIPE_LEFT)} aria-label="Skip this role">
          <IconX />
        </button>
        <button
          type="button"
          className="swipe apply"
          onClick={() => commit(SWIPE_RIGHT)}
          aria-label="Apply to this role"
        >
          <IconCheck />
        </button>
      </div>
      <p className="deck-hint">Swipe, tap, or use the arrow keys</p>
    </>
  );
}
