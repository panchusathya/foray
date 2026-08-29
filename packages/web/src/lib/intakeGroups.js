/**
 * Collapse a flat list of intake calls into one entry per caller.
 *
 * The API returns calls in time order with no grouping (there is no
 * IntakeSession yet), so the same person ringing three times reads as three
 * unrelated rows -- and because `contact_name` is captured per call, the rows
 * that happened to get a name look like a different person from the ones that
 * did not.
 */

/**
 * The number of the person on the other end, whichever way we dialled. Mirrors
 * ``counterparty_number`` in juicebox/intake/service.py: on an outbound call
 * ``from_number`` is *our* intake line, so reading it as the candidate's shows
 * Foray's own number as the caller.
 */
export function counterpartyNumber(call) {
  return (call.entry_point === 'outbound' ? call.to_number : call.from_number) || null;
}

function latestOf(call) {
  return call.started_at || call.created_at || '';
}

/**
 * Group calls by caller.
 *
 * Attributed calls key on `candidate_id`. Unattributed ones key on the
 * counterparty number, then fold into the candidate group if any *other* call
 * from that number was attributed -- otherwise one person shows up twice purely
 * because attribution landed on their second call.
 */
export function groupCalls(rows) {
  const calls = [...(rows ?? [])];

  // Which candidate a number is known to belong to, learned from any attributed
  // call. Built first so an unattributed call earlier in the list still folds in.
  const candidateByNumber = new Map();
  for (const call of calls) {
    const number = counterpartyNumber(call);
    if (call.candidate_id && number && !candidateByNumber.has(number)) {
      candidateByNumber.set(number, call.candidate_id);
    }
  }

  const groups = new Map();
  for (const call of calls) {
    const number = counterpartyNumber(call);
    const candidateId = call.candidate_id || (number ? candidateByNumber.get(number) : null);
    const key = candidateId ? `candidate:${candidateId}` : `number:${number ?? call.id}`;

    let group = groups.get(key);
    if (!group) {
      group = {
        key,
        candidateId: candidateId ?? null,
        number: number ?? null,
        contactName: null,
        contactEmail: null,
        calls: [],
      };
      groups.set(key, group);
    }
    // Best value any call in the set carries -- a name captured on the third
    // call names the whole group.
    if (!group.contactName && call.contact_name) group.contactName = call.contact_name;
    if (!group.contactEmail && call.contact_email) group.contactEmail = call.contact_email;
    if (!group.number && number) group.number = number;
    group.calls.push(call);
  }

  for (const group of groups.values()) {
    group.calls.sort((a, b) => latestOf(b).localeCompare(latestOf(a)));
    group.latest = latestOf(group.calls[0]);
    group.reviewCount = group.calls.filter((call) => call.review_needed).length;
  }

  return [...groups.values()].sort((a, b) => (b.latest ?? '').localeCompare(a.latest ?? ''));
}
