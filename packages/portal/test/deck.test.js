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
} from '../src/lib/deck.js';

const CARDS = [
  { posting_id: 'p1', title: 'ML Engineer', company: 'Anthropic' },
  { posting_id: 'p2', title: 'Backend Engineer', company: 'Figma' },
];

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
