import assert from 'node:assert/strict';
import { test } from 'node:test';

import { forayHitToCandidate, partitionByEmail } from '../src/lib/forayImport.js';

const HIT = {
  login: 'janedoe',
  name: 'Jane Doe',
  github_url: 'https://github.com/janedoe',
  linkedin_url: 'https://linkedin.com/in/janedoe',
  location: 'San Francisco',
  employer: 'Stripe',
  employer_domain: 'stripe.com',
  est_experience_years: 7.2,
  primary_language: 'go',
  domain_markers: ['infra'],
  topics: ['kubernetes'],
  emails: [
    { email: 'jane@stripe.com', source: 'profile', is_work: true },
    { email: 'jd@gmail.com', source: 'commit', is_work: false },
  ],
  score: 71,
  highlights: ['x'],
  flags: [],
};

test('maps core fields with login as source_id', () => {
  const c = forayHitToCandidate(HIT, { archetypeId: 'infra_platform' });
  assert.equal(c.source, 'foray');
  assert.equal(c.source_id, 'janedoe');
  assert.equal(c.full_name, 'Jane Doe');
  assert.equal(c.current_company, 'Stripe');
  assert.equal(c.current_company_domain, 'stripe.com');
  assert.equal(c.years_experience, 7.2);
  assert.deepEqual(c.emails, ['jane@stripe.com', 'jd@gmail.com']);
  assert.deepEqual(c.skills, ['go', 'infra', 'kubernetes']);
  assert.equal(c.raw.foray.score, 71);
  assert.equal(c.raw.foray.archetype, 'infra_platform');
});

test('full_name falls back to login', () => {
  const c = forayHitToCandidate({ login: 'ghost', emails: [] });
  assert.equal(c.full_name, 'ghost');
  assert.deepEqual(c.emails, []);
  assert.equal(c.years_experience, null);
});

test('partitionByEmail splits contactable from not', () => {
  const { withEmail, withoutEmail } = partitionByEmail([
    HIT,
    { login: 'noemail', emails: [] },
    { login: 'nullemails' },
  ]);
  assert.deepEqual(withEmail.map((h) => h.login), ['janedoe']);
  assert.deepEqual(withoutEmail.map((h) => h.login), ['noemail', 'nullemails']);
});
