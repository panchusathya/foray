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

export function currentCard(state) {
  return state.cards[state.index] || null;
}

export function upcoming(state, count = 2) {
  return state.cards.slice(state.index + 1, state.index + 1 + count);
}

export function isExhausted(state) {
  return state.index >= state.cards.length;
}

export function deckReducer(state, action) {
  switch (action.type) {
    case 'load':
      return initialDeck(action.cards);

    case 'swipe': {
      const card = currentCard(state);
      if (!card) return state;
      // Advancing before the server answers is what makes the deck feel
      // native; `pending` is how a failure finds its way back to the card.
      return {
        ...state,
        index: state.index + 1,
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
