import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { malformedInStep, malformedPlaceholders } from '../src/lib/templates.js';

// These cases are the same ones pinned in packages/core/tests/test_malformed_templates.py.
// The two implementations have to agree: this one decides whether the recruiter
// sees a warning, that one decides whether the email is allowed to send, and a
// warning that does not match the refusal is worse than no warning.
describe('malformedPlaceholders', () => {
  it('leaves valid variables and directives alone', () => {
    for (const template of [
      'Hi {{ first_name }},',
      'Hi {{ first_name | there }},',
      '{{ai: say why this fits}}',
      '{{ AI : mention their stack }}',
      'Plain copy with no placeholders at all.',
      '',
      null,
      undefined,
    ]) {
      assert.deepEqual(malformedPlaceholders(template), [], `for ${JSON.stringify(template)}`);
    }
  });

  it('reports an unterminated placeholder, which has no braces to match on', () => {
    assert.deepEqual(malformedPlaceholders('Hi {{ first_name,'), ['{{ first_name,']);
    assert.deepEqual(malformedPlaceholders('{{ first_name\nnext line'), ['{{ first_name']);
  });
});

describe('malformedInStep', () => {
  it('covers the subject as well as the body', () => {
    assert.deepEqual(
      malformedInStep({ subject_template: 'A role for {{First name}}', body_template: 'Hi {{ first_name }},' }),
      ['{{First name}}'],
    );
  });
});
