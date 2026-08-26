import assert from 'node:assert/strict';
import test from 'node:test';

import {
  SWIPE_LEFT,
  SWIPE_RIGHT,
  currentCard,
  deckReducer,
  directionFor,
  initialDeck,
  isExhausted,
  upcoming,
} from '../src/lib/deck.js';

const CARDS = [
  { posting_id: 'p1', title: 'ML Engineer', company: 'Anthropic' },
  { posting_id: 'p2', title: 'Backend Engineer', company: 'Figma' },
];

const THREE = [...CARDS, { posting_id: 'p3', title: 'Platform Engineer', company: 'Stripe' }];

test('a swipe advances immediately and remembers what is in flight', () => {
  let state = initialDeck(CARDS);
  assert.equal(currentCard(state).posting_id, 'p1');

  state = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  // The deck must feel instant, so the next card is already up while the
  // request is still going.
  assert.equal(currentCard(state).posting_id, 'p2');
  assert.equal(state.pending.p1, SWIPE_RIGHT);

  state = deckReducer(state, { type: 'settled', postingId: 'p1', status: 'accepted' });
  assert.equal(state.pending.p1, undefined);
  assert.equal(state.answered.p1, 'accepted');
});

test('a failed swipe puts the card back rather than swallowing it', () => {
  let state = initialDeck(CARDS);
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  state = deckReducer(state, { type: 'failed', postingId: 'p1' });
  // A silently dropped "apply" is the worst thing this screen can do.
  assert.equal(currentCard(state).posting_id, 'p1');
  assert.equal(state.pending.p1, undefined);
});

test('a failure rewinds over answered cards without re-presenting them', () => {
  // Three quick swipes on a bad connection: the first one's POST fails, the
  // other two came back accepted and have applications being written.
  let state = initialDeck(THREE);
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  state = deckReducer(state, { type: 'settled', postingId: 'p2', status: 'accepted' });
  state = deckReducer(state, { type: 'settled', postingId: 'p3', status: 'accepted' });
  state = deckReducer(state, { type: 'failed', postingId: 'p1' });

  // Only the one that did not land comes back.
  assert.equal(currentCard(state).posting_id, 'p1');
  assert.deepEqual(upcoming(state, 2), []);

  // Re-swiping it does not walk back into p2/p3 and post them twice.
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  assert.equal(currentCard(state), null);
  assert.equal(isExhausted(state), true);
});

test('a card still in flight is not re-presented by someone else failing', () => {
  let state = initialDeck(THREE);
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_LEFT });
  // p2 is still going; only p1 came back with a failure.
  state = deckReducer(state, { type: 'failed', postingId: 'p1' });
  assert.equal(currentCard(state).posting_id, 'p1');
  assert.deepEqual(
    upcoming(state, 2).map((card) => card.posting_id),
    ['p3'],
  );

  // ...and if p2 fails too, it comes back in its own right.
  state = deckReducer(state, { type: 'failed', postingId: 'p2' });
  assert.equal(currentCard(state).posting_id, 'p1');
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  assert.equal(currentCard(state).posting_id, 'p2');
});

test('a parked swipe is recorded, not lost', () => {
  let state = initialDeck(CARDS);
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  state = deckReducer(state, { type: 'settled', postingId: 'p1', status: 'parked' });
  assert.deepEqual(state.parked, ['p1']);
  assert.equal(state.answered.p1, 'parked');
});

test('the deck runs out exactly once', () => {
  let state = initialDeck(CARDS);
  assert.equal(isExhausted(state), false);
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_LEFT });
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_LEFT });
  assert.equal(isExhausted(state), true);
  assert.equal(currentCard(state), null);
  // A swipe past the end is a no-op, not a crash or a phantom application.
  const after = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  assert.equal(after, state);
});

test('a short drag is not an application', () => {
  assert.equal(directionFor(20), null);
  assert.equal(directionFor(-20), null);
  assert.equal(directionFor(120), SWIPE_RIGHT);
  assert.equal(directionFor(-120), SWIPE_LEFT);
});

test('loading a new feed resets everything', () => {
  let state = initialDeck(CARDS);
  state = deckReducer(state, { type: 'swipe', direction: SWIPE_RIGHT });
  state = deckReducer(state, { type: 'load', cards: [CARDS[1]] });
  assert.equal(state.index, 0);
  assert.deepEqual(state.pending, {});
  assert.equal(currentCard(state).posting_id, 'p2');
});
