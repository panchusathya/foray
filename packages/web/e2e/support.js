/**
 * Shared setup. Data arrives through the API (fast and deterministic); the
 * assertions that matter happen through the UI.
 *
 * Every spec signs up its own org, so specs never see each other's candidates
 * and a failure points at one flow rather than at test ordering.
 */

let orgCounter = 0;

export const PASSWORD = 'e2e-password-9182';

export function uniqueEmail(prefix) {
  orgCounter += 1;
  // Unique per run AND per call: the database outlives a CI job when someone
  // reuses a staging stack, and a duplicate signup is a 409, not a fresh org.
  //
  // The domain must be a real-looking one. Pydantic's EmailStr rejects the
  // special-use TLDs (.invalid, .test, .example, .localhost) that would
  // otherwise be the obvious choice — a subdomain of example.com is both
  // accepted and unroutable.
  return `${prefix}-${Date.now()}-${orgCounter}@e2e.example.com`;
}

/** Sign up a fresh org and return {email, token, headers}. */
export async function createOrg(request, prefix = 'e2e') {
  const email = uniqueEmail(prefix);
  const response = await request.post('/v1/auth/signup', {
    data: { org_name: `E2E ${prefix} ${orgCounter}`, email, password: PASSWORD },
  });
  if (!response.ok()) {
    throw new Error(`signup failed (${response.status()}): ${await response.text()}`);
  }
  const body = await response.json();
  return {
    email,
    token: body.access_token,
    headers: { Authorization: `Bearer ${body.access_token}` },
  };
}

/** A small candidate set with predictable names, titles and emails. */
export function candidateFixtures(tag) {
  return [
    {
      source: 'e2e',
      source_id: `${tag}-1`,
      full_name: `Ada ${tag}`,
      emails: [`ada-${tag}@e2e.example.com`],
      current_title: 'Senior Machine Learning Engineer',
      current_company: 'Modal',
      location: 'San Francisco, CA',
      skills: ['python', 'pytorch', 'llm'],
      years_experience: 7,
    },
    {
      source: 'e2e',
      source_id: `${tag}-2`,
      full_name: `Grace ${tag}`,
      emails: [`grace-${tag}@e2e.example.com`],
      current_title: 'Staff Machine Learning Engineer',
      current_company: 'Tailscale',
      location: 'San Francisco, CA',
      skills: ['python', 'kubernetes'],
      years_experience: 9,
    },
    {
      // Deliberately contactless: the UI must warn rather than silently enrol
      // someone who can never receive a step.
      source: 'e2e',
      source_id: `${tag}-3`,
      full_name: `Noemail ${tag}`,
      emails: [],
      current_title: 'Machine Learning Engineer',
      current_company: 'Benchling',
      location: 'San Francisco, CA',
      skills: ['python'],
      years_experience: 5,
    },
  ];
}

export async function seedCandidates(request, org, tag) {
  const response = await request.post('/v1/candidates/bulk', {
    headers: org.headers,
    data: { candidates: candidateFixtures(tag) },
  });
  if (!response.ok()) {
    throw new Error(`bulk upsert failed (${response.status()}): ${await response.text()}`);
  }
  return (await response.json()).ids;
}

export async function createProject(request, org, name = 'E2E Role') {
  const response = await request.post('/v1/projects', {
    headers: org.headers,
    data: { name, role_title: 'Machine Learning Engineer', location: 'San Francisco' },
  });
  if (!response.ok()) {
    throw new Error(`create project failed (${response.status()}): ${await response.text()}`);
  }
  return response.json();
}

export async function addToProject(request, org, projectId, candidateIds) {
  const response = await request.post(`/v1/projects/${projectId}/candidates`, {
    headers: org.headers,
    data: { candidate_ids: candidateIds },
  });
  if (!response.ok()) {
    throw new Error(`add to project failed (${response.status()}): ${await response.text()}`);
  }
  return response.json();
}

export async function createSequence(request, org, projectId, overrides = {}) {
  const response = await request.post('/v1/sequences', {
    headers: org.headers,
    data: {
      name: 'E2E outreach',
      project_id: projectId,
      steps: [
        {
          delay_days: 0,
          subject_template: 'Quick note for {{ first_name }}',
          body_template: 'Hi {{ first_name }}, about {{ role_title | the role }}.',
        },
      ],
      ...overrides,
    },
  });
  if (!response.ok()) {
    throw new Error(`create sequence failed (${response.status()}): ${await response.text()}`);
  }
  return response.json();
}

/**
 * Log in through the UI. Deliberately not a token injection: the login form is
 * the one flow every user hits, so exercising it here keeps it covered.
 */
export async function login(page, email) {
  await page.goto('/');
  await page.getByPlaceholder('Email').fill(email);
  await page.getByPlaceholder('Password').fill(PASSWORD);
  // The form has a "Log in" tab button as well as the submit; target the
  // submit explicitly so a tab-order change cannot silently break every spec.
  await page.locator('button[type="submit"]').click();
  await page.waitForSelector('nav', { timeout: 15_000 });
}
