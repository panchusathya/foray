import { expect, test } from '@playwright/test';

import { createOrg, login, seedCandidates } from './support.js';

test.describe('jobs tab', () => {
  test('a recruiter can add a posting and see it in the grid', async ({ page, request }) => {
    const org = await createOrg(request, 'jobs');
    await seedCandidates(request, org, 'jobs');
    await login(page, org.email);

    await page.goto('/jobs');
    await expect(page.getByTestId('job-create-form')).toBeVisible();

    await page.getByPlaceholder('Title', { exact: true }).fill('Senior Machine Learning Engineer');
    await page.getByPlaceholder('Company', { exact: true }).fill('Acme');
    await page.getByPlaceholder('Location').fill('San Francisco');
    await page.getByPlaceholder('Job description').fill('Python and ranking in production.');
    await page.getByRole('button', { name: 'Add posting' }).click();

    await expect(page.getByTestId('jobs-table')).toBeVisible();
    await expect(page.getByTestId('job-row').first()).toBeVisible();
    await expect(page.getByTestId('job-row')).toHaveCount(1);
    await expect(page.getByText('Senior Machine Learning Engineer')).toBeVisible();
    await expect(page.getByTestId('job-source')).toHaveText('Manual');
    await expect(page.getByTestId('pitch-cta')).toHaveText('Create project to pitch');
    await expect(page.getByTestId('jobs-source-filter')).toBeVisible();

    await page.getByTestId('pitch-cta').click();
    await expect(page.getByRole('heading', { name: 'Sequences' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Create sequence' })).toBeVisible();
  });
});
