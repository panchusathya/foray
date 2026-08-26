import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { malformedInStep, malformedPlaceholders } from '../src/lib/templates.js';

// These cases are the same ones pinned in packages/core/tests/test_malformed_templates.py.
// The two implementations have to agree: this one decides whether the recruiter
// sees a warning, that one decides whether the email is allowed to send, and a
// warning that does not match the refusal is worse than no warning.
describe('malformedPlaceholders', () => {
  it('reports a variable name with a space in it', () => {
    // The one that actually shipped: "Hi {{First name}}," reached a candidate.
    assert.deepEqual(malformedPlaceholders('Hi {{First name}},'), ['{{First name}}']);
    assert.deepEqual(malformedPlaceholders('Hi {{ First name }},'), ['{{ First name }}']);
    assert.deepEqual(malformedPlaceholders('Hi {{first name}},'), ['{{first name}}']);
  });

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

  it('ellipsises only what it actually cut', () => {
    const [found] = malformedPlaceholders(`{{ ${'x'.repeat(200)}\nnext line`);
    assert.ok(found.endsWith('…'));
    assert.ok(!found.includes('\n'));
  });

  it('isolates a broken placeholder from the good ones around it', () => {
    assert.deepEqual(
      malformedPlaceholders('Hi {{ first_name }}, {{Bad name}} at {{ current_company }}'),
      ['{{Bad name}}'],
    );
  });

  it('reports a repeat once, in first-seen order', () => {
    assert.deepEqual(malformedPlaceholders('{{B x}} {{A y}} {{B x}}'), ['{{B x}}', '{{A y}}']);
  });
});

describe('malformedInStep', () => {
  it('covers the subject as well as the body', () => {
    assert.deepEqual(
      malformedInStep({ subject_template: 'A role for {{First name}}', body_template: 'Hi {{ first_name }},' }),
      ['{{First name}}'],
    );
  });

  it('does not report the same placeholder twice across the two fields', () => {
    assert.deepEqual(
      malformedInStep({ subject_template: '{{First name}}', body_template: 'Hi {{First name}},' }),
      ['{{First name}}'],
    );
  });

  it('is quiet for a clean step, and for no step at all', () => {
    assert.deepEqual(
      malformedInStep({ subject_template: '{{ role_title }}', body_template: 'Hi {{ first_name | there }},' }),
      [],
    );
    assert.deepEqual(malformedInStep(undefined), []);
  });
});
