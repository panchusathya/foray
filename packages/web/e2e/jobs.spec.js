import { expect, test } from '@playwright/test';

import { createOrg, login, seedCandidates } from './support.js';

test.describe('jobs tab', () => {
  test('overview is the landing view and add lives behind a tab', async ({ page, request }) => {
    const org = await createOrg(request, 'jobs');
    await seedCandidates(request, org, 'jobs');
    await login(page, org.email);

    await page.goto('/jobs');
    await expect(page.getByTestId('view-tabs')).toBeVisible();
    await expect(page.getByTestId('job-create-form')).toHaveCount(0);
    await expect(page.getByText('No job postings yet.')).toBeVisible();

    await page.getByTestId('view-tabs').getByRole('link', { name: 'Add' }).click();
    await expect(page).toHaveURL(/view=add/);
    await expect(page.getByTestId('job-create-form')).toBeVisible();

    await page.getByPlaceholder('Title', { exact: true }).fill('Senior Machine Learning Engineer');
    await page.getByPlaceholder('Company', { exact: true }).fill('Acme');
    await page.getByPlaceholder('Location').fill('San Francisco');
    await page.getByPlaceholder('Job description').fill('Python and ranking in production.');
    await page.getByRole('button', { name: 'Add posting' }).click();

    await expect(page.getByRole('link', { name: /View it in Overview/ })).toBeVisible();
    await page.getByRole('link', { name: /View it in Overview/ }).click();

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
