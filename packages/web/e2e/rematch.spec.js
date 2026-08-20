import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

test.describe('rematch tab', () => {
  test('the rematch queue renders even when empty', async ({ page, request }) => {
    const org = await createOrg(request, 'rematch');
    await login(page, org.email);
    await page.goto('/rematch');
    await expect(page).toHaveURL(/\/jobs\?view=rematch/);
    await expect(page.getByTestId('view-tabs').getByRole('link', { name: 'Rematch' })).toBeVisible();
    await expect(page.getByText('Nobody in the rematch queue.')).toBeVisible();
  });
});
