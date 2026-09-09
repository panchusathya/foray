import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

/**
 * Outbox and the org sending kill switch.
 *
 * Populated in-flight rows need queued send jobs the browser cannot seed, so
 * those cases live in packages/core/tests/test_outbox.py. This covers the
 * surfaces a recruiter hits with an empty org: the nav entry, the empty
 * page, and the Settings toggle that paints the banner.
 */
test.describe('outbox', () => {
  test('the nav lists Outbox with the other org-wide placement pages', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'outbox-nav');
    await login(page, org.email);
    await page.goto('/');

    const group = page.locator('.sidebar-section', { hasText: 'PLACE PEOPLE' });
    await expect(group.getByRole('link', { name: 'Outbox', exact: true })).toBeVisible();
    await group.getByRole('link', { name: 'Outbox', exact: true }).click();
    await expect(page).toHaveURL(/\/outbox$/);
    await expect(page.getByRole('heading', { name: 'Outbox' })).toBeVisible();
    await expect(page.getByText('Nothing in flight.')).toBeVisible();
  });

  test('the kill switch pauses sending and the banner appears', async ({ page, request }) => {
    const org = await createOrg(request, 'outbox-kill');
    await login(page, org.email);
    await page.goto('/settings');

    const toggle = page.getByTestId('org-sending-toggle');
    await expect(toggle).toHaveText('Sending: on');
    await expect(page.getByTestId('sending-paused-banner')).toHaveCount(0);

    page.once('dialog', (dialog) => dialog.accept());
    await toggle.click();
    await expect(toggle).toHaveText('Sending: off');
    await expect(page.getByTestId('sending-paused-banner')).toBeVisible();
    await expect(page.getByTestId('sending-paused-banner')).toContainText('Sending paused');

    await toggle.click();
    await expect(toggle).toHaveText('Sending: on');
    await expect(page.getByTestId('sending-paused-banner')).toHaveCount(0);
  });
});
