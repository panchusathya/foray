import { expect, test } from '@playwright/test';

import { createOrg, login, seedCandidates } from './support.js';

test.describe('intake', () => {
  test('the page and the contacts view exist', async ({ page, request }) => {
    const org = await createOrg(request, 'intake');
    await seedCandidates(request, org, 'intake');
    await login(page, org.email);

    await page.goto('/intake');
    await expect(page.getByRole('heading', { name: 'Intake' })).toBeVisible();
    await expect(page.getByTestId('view-tabs')).toBeVisible();
    await expect(page.getByTestId('view-tabs').getByRole('link', { name: 'Calls' })).toBeVisible();
    await expect(page.getByTestId('intake-calls')).toBeVisible();
    await expect(page.getByText('No intake calls yet.')).toBeVisible();

    await page.getByTestId('view-tabs').getByRole('link', { name: 'Review queue' }).click();
    await expect(page).toHaveURL(/view=review/);
    await expect(page.getByTestId('intake-review')).toBeVisible();

    await page.getByTestId('view-tabs').getByRole('link', { name: 'Job matches' }).click();
    await expect(page).toHaveURL(/view=matches/);
    await expect(page.getByTestId('intake-matches')).toBeVisible();

    await page.goto('/contacts?view=voice_intake');
    await expect(page.getByTestId('view-tabs').getByRole('link', { name: 'Voice intake' })).toBeVisible();
    await expect(page.getByText('Nobody has completed an intake call yet.')).toBeVisible();
  });

  test('the person record has intake and jobs tabs', async ({ page, request }) => {
    const org = await createOrg(request, 'intake-rec');
    const ids = await seedCandidates(request, org, 'intake-rec');
    await login(page, org.email);
    await page.goto(`/contacts/${ids[0]}`);

    await expect(page.getByTestId('tab-intake')).toBeVisible();
    await expect(page.getByTestId('tab-jobs')).toBeVisible();
    await page.getByTestId('tab-intake').click();
    await expect(page.getByText('No intake record for this person yet.')).toBeVisible();
    await page.getByTestId('tab-jobs').click();
    await expect(page.getByTestId('jobs-panel')).toBeVisible();
  });
});
