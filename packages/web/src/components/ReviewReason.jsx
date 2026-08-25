/**
 * Why a record is held back, rendered as separate concerns rather than one
 * run-on sentence of field keys. Parsing lives in `lib/reviewReason.js`.
 *
 * Five places show this string (calls, texts, both review queues, the contact
 * record), so it is a component: the moment it is inlined twice, one of them
 * gets a fix the other does not.
 */

import { parseReviewReason } from '../lib/reviewReason.js';

export function ReviewReason({ reason, fallback = 'Needs a look.' }) {
  const groups = parseReviewReason(reason);
  if (!groups.length) return <div className="small muted">{fallback}</div>;

  return (
    <div className="review-reason" data-testid="review-reason">
      {groups.map((group, index) => (
        <div className="review-clause" key={`${group.title}-${index}`}>
          <span className={`review-marker ${group.tone}`} aria-hidden="true" />
          <div>
            <span className="small">{group.title}</span>
            {group.note ? <div className="muted small">{group.note}</div> : null}
            {group.fields.length ? (
              <div className="review-fields">
                {group.fields.map((field) => (
                  <span className="badge" key={field}>
                    {field}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ))}
    </div>
  );
}
