import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

const STUB_TURN = {
  reply: '2 postings below. Want me to widen to remote?',
  questions: [],
  assumptions: [],
  steps: [
    { label: 'staff ml engineer SF', detail: 'Exa search', count: 2 },
    { label: 'deduped', detail: '2 new, 0 already yours', count: 2 },
  ],
  results: [
    {
      source_id: 'disc-1',
      title: 'Staff Platform Engineer',
      company: 'Datadog',
      location: 'New York',
      jd_text: 'Own the control plane.',
      source_url: 'https://jobs.example.com/staff-platform-discover',
      already_have: false,
      posting_id: null,
    },
    {
      source_id: 'disc-2',
      title: 'AI Infra Engineer',
      company: 'Anthropic',
      location: 'San Francisco',
      jd_text: 'CUDA and Kubernetes.',
      source_url: 'https://jobs.example.com/ai-infra-discover',
      already_have: false,
      posting_id: null,
    },
  ],
};

test.describe('jobs discover', () => {
  test('the four tabs render and ?view= switches without leaving /jobs', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'jobs-tabs');
    await login(page, org.email);
    await page.goto('/jobs');
    const tabs = page.getByTestId('view-tabs');
    await expect(tabs.getByRole('link', { name: 'Overview' })).toBeVisible();
    await expect(tabs.getByRole('link', { name: 'Add' })).toBeVisible();
    await expect(tabs.getByRole('link', { name: 'Discover' })).toBeVisible();
    await expect(tabs.getByRole('link', { name: 'Rematch' })).toBeVisible();

    await tabs.getByRole('link', { name: 'Discover' }).click();
    await expect(page).toHaveURL(/\/jobs\?view=discover/);
    await expect(page.getByTestId('discover-composer')).toBeVisible();
    await expect(page).toHaveURL(/\/jobs/);
  });

  test('/rematch redirects into the Jobs rematch view', async ({ page, request }) => {
    const org = await createOrg(request, 'jobs-redirect');
    await login(page, org.email);
    await page.goto('/rematch');
    await expect(page).toHaveURL(/\/jobs\?view=rematch/);
    await expect(page.getByText('Nobody in the rematch queue.')).toBeVisible();
  });

  test('a stubbed turn renders steps, results, and Add lands in Overview', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'jobs-discover');
    await login(page, org.email);
    await page.route('**/v1/job-discovery/turns', async (route) => {
      await route.fulfill({ json: STUB_TURN });
    });

    await page.goto('/jobs?view=discover');
    await page.getByTestId('discover-composer').locator('textarea').fill('staff ML in SF');
    await page.getByTestId('discover-send').click();

    await expect(page.getByTestId('discover-steps')).toBeVisible();
    await expect(page.getByTestId('discover-results')).toBeVisible();
    await expect(page.getByTestId('discover-row')).toHaveCount(2);
    await expect(page.getByText('Staff Platform Engineer')).toBeVisible();

    await page.getByRole('checkbox', { name: 'Select Staff Platform Engineer' }).check();
    await expect(page.getByTestId('discover-add')).toBeVisible();
    await page.getByRole('button', { name: 'Add to Jobs' }).click();
    await expect(page.getByRole('link', { name: /View it in Overview/ })).toBeVisible();

    await page.getByTestId('view-tabs').getByRole('link', { name: 'Overview' }).click();
    await expect(page.getByTestId('jobs-table')).toBeVisible();
    await expect(page.getByText('Staff Platform Engineer')).toBeVisible();
    await expect(page.getByTestId('job-source')).toHaveText('Exa');
  });
});
