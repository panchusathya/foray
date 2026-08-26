// The swipe deck's state, with no DOM and no gestures in sight.
//
// Split out on purpose: gesture handling is untestable without a browser,
// while "what happens when a card is answered" is the part that can silently
// lose somebody's application. This half is a pure reducer and is unit-tested
// (test/deck.test.js).

export const SWIPE_RIGHT = 'right';
export const SWIPE_LEFT = 'left';

export function initialDeck(cards = []) {
  return {
    cards,
    index: 0,
    // Outcomes keyed by posting id, so a re-render or a late server reply
    // cannot double-count a card.
    answered: {},
    // Swipes whose request is still in flight; the UI advances immediately
    // (the deck must feel instant) and reconciles when each settles.
    pending: {},
    parked: [],
  };
}

// A card is spoken for once its swipe has settled, and while its request is
// still going. Both matter for the rewind below: a failure rewinds the index
// over cards that were swiped after it, and re-presenting those would post a
// second swipe for an application already being written.
function spokenFor(state, card) {
  return card.posting_id in state.answered || card.posting_id in state.pending;
}

// The index is a floor, not the answer: `failed` moves it back over cards that
// may already be settled, so walk forward to the first one still open.
function activeIndex(state) {
  let index = state.index;
  while (index < state.cards.length && spokenFor(state, state.cards[index])) index += 1;
  return index;
}

export function currentCard(state) {
  return state.cards[activeIndex(state)] || null;
}

export function upcoming(state, count = 2) {
  const ahead = [];
  for (let i = activeIndex(state) + 1; i < state.cards.length && ahead.length < count; i += 1) {
    if (!spokenFor(state, state.cards[i])) ahead.push(state.cards[i]);
  }
  return ahead;
}

export function isExhausted(state) {
  return activeIndex(state) >= state.cards.length;
}

export function deckReducer(state, action) {
  switch (action.type) {
    case 'load':
      return initialDeck(action.cards);

    case 'swipe': {
      const at = activeIndex(state);
      const card = state.cards[at];
      if (!card) return state;
      // Advancing before the server answers is what makes the deck feel
      // native; `pending` is how a failure finds its way back to the card.
      return {
        ...state,
        index: at + 1,
        pending: { ...state.pending, [card.posting_id]: action.direction },
      };
    }

    case 'settled': {
      const { postingId, status } = action;
      const pending = { ...state.pending };
      delete pending[postingId];
      return {
        ...state,
        pending,
        answered: { ...state.answered, [postingId]: status },
        parked: status === 'parked' ? [...state.parked, postingId] : state.parked,
      };
    }

    case 'failed': {
      // The swipe did not land. Put the card back where it was rather than
      // pretending it was answered -- a silently dropped "apply" is the worst
      // outcome this screen can produce.
      //
      // Only this card comes back: the index rewinds over everything swiped
      // after it, but currentCard skips whatever has already settled or is
      // still in flight, so a failure on the first of three quick swipes does
      // not re-present the two whose applications are already being written.
      const pending = { ...state.pending };
      delete pending[action.postingId];
      const position = state.cards.findIndex((card) => card.posting_id === action.postingId);
      return {
        ...state,
        pending,
        index: position >= 0 ? Math.min(state.index, position) : state.index,
      };
    }

    default:
      return state;
  }
}

// How far a drag must travel before it counts. Below this the card springs
// back: a scroll that starts slightly sideways is not an application.
export const SWIPE_THRESHOLD_PX = 90;

export function directionFor(deltaX, threshold = SWIPE_THRESHOLD_PX) {
  if (deltaX >= threshold) return SWIPE_RIGHT;
  if (deltaX <= -threshold) return SWIPE_LEFT;
  return null;
}
