/**
 * The Home to-do list: what each `/v1/analytics/todo` key means, and where
 * clicking it goes.
 *
 * The API returns keys, counts and ages — never sentences. Copy lives here so
 * rewording a line is a web deploy, and so a key the server starts emitting
 * before this file knows about it degrades to a readable row instead of
 * blanking the list.
 *
 * Order in this file is the order on the page: replies first, because a person
 * waiting on an answer is the only item here with someone on the other end of
 * it.
 */

export const TODO_ITEMS = {
  replies_waiting: {
    order: 0,
    to: '/contacts?view=replied',
    title: (count) => `${count} ${count === 1 ? 'reply is' : 'replies are'} waiting on you`,
    detail: (item) => ageLine(item.oldest_hours),
  },
  packages_failed: {
    order: 1,
    to: '/applications?view=all',
    title: (count) =>
      `${count} application ${count === 1 ? 'package' : 'packages'} failed to build`,
    detail: () => 'Nothing can be sent until the package exists.',
  },
  sends_failed: {
    order: 2,
    to: '/sequences',
    title: (count) => `${count} ${count === 1 ? 'email' : 'emails'} never got through`,
    detail: () => 'Failed or bounced. The sequence moved on without them.',
  },
  applications_awaiting_consent: {
    order: 3,
    to: '/applications?view=review',
    title: (count) => `${count} ${count === 1 ? 'application is' : 'applications are'} waiting on a candidate`,
    detail: () => 'Packaged and ready — nothing goes out until they say yes.',
  },
  applications_prepared: {
    order: 4,
    to: '/applications?view=review',
    title: (count) => `${count} ${count === 1 ? 'package is' : 'packages are'} built and unsent`,
    detail: () => 'Ready to go out as soon as the yes is on file.',
  },
  intake_review: {
    order: 5,
    to: '/intake?view=review',
    title: (count) => `${count} intake ${count === 1 ? 'conversation needs' : 'conversations need'} a look`,
    detail: () => 'Unattributed callers, or fields the extractor was unsure about.',
  },
};

/**
 * How old the oldest unanswered reply is, in the units a person would say it
 * in. Under a day never appears — the API already holds those back.
 */
export function ageLine(hours) {
  if (hours == null) return '';
  const days = Math.floor(hours / 24);
  if (days >= 14) return `The oldest has been sitting for ${Math.floor(days / 7)} weeks.`;
  if (days >= 1) return `The oldest has been sitting for ${days} ${days === 1 ? 'day' : 'days'}.`;
  return `The oldest came in ${Math.round(hours)} hours ago.`;
}

/**
 * Server rows -> render-ready rows. An unknown key still renders (its own
 * name, no link) rather than vanishing, because a silently dropped item is
 * indistinguishable from having nothing to do.
 */
export function todoRows(items = []) {
  return items
    .filter((item) => (item?.count ?? 0) > 0)
    .map((item) => {
      const spec = TODO_ITEMS[item.key];
      if (!spec) {
        return { key: item.key, order: 99, count: item.count, title: item.key, detail: '', to: null, tone: item.tone };
      }
      return {
        key: item.key,
        order: spec.order,
        count: item.count,
        title: spec.title(item.count),
        detail: spec.detail(item),
        to: spec.to,
        tone: item.tone,
      };
    })
    .sort((a, b) => a.order - b.order);
}
