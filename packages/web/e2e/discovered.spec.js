import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

/**
 * The Discovered tray.
 *
 * Staging rows requires a connected mailbox and a provider to sweep, neither
 * of which a browser test can produce — OAuth cannot be driven from here, the
 * same reason mailboxes.spec.js defers its populated cases. So promote,
 * dismiss and the filters are covered by packages/core/tests/test_discovery.py
 * against a fake provider, and what is asserted here is the part that only
 * exists in the browser: that the tray is reachable, framed as part of
 * Contacts, and says something useful when empty.
 */
test.describe('discovered tray', () => {
  test('is a view of Contacts, not a separate app', async ({ page, request }) => {
    // The whole shape of this feature was chosen so it would not become its
    // own nav entry; that is worth a cheap guard.
    const org = await createOrg(request, 'tray-nav');

    await login(page, org.email);
    await page.goto('/contacts');

    const sidebar = page.locator('.sidebar');
    await expect(sidebar.getByRole('link', { name: 'Discovered' })).toHaveCount(0);

    await page.getByTestId('view-tabs').getByRole('link', { name: 'Discovered' }).click();
    await expect(page).toHaveURL(/view=discovered/);
    await expect(page.getByTestId('discovered-tray')).toBeVisible();
  });

  test('an empty tray says where sweeps come from', async ({ page, request }) => {
    const org = await createOrg(request, 'tray-empty');

    await login(page, org.email);
    await page.goto('/contacts?view=discovered');

    const tray = page.getByTestId('discovered-tray');
    await expect(tray).toContainText('Nothing to review');
    await expect(tray.getByRole('link', { name: 'Settings' })).toBeVisible();
    // Nothing selected, so no destructive controls are on screen.
    await expect(page.getByTestId('tray-actions')).toHaveCount(0);
  });

  test('the review filters are present and default to the useful ones', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'tray-filters');

    await login(page, org.email);
    await page.goto('/contacts?view=discovered');

    // New only: mixing in people already in Contacts would bury the strangers
    // the sweep exists to surface.
    await expect(page.getByTestId('tray-status')).toHaveValue('new');
    // Cc-only is the weakest signal, hidden until asked for.
    await expect(page.getByTestId('tray-cc-only')).not.toBeChecked();
    await expect(page.getByTestId('tray-min-messages')).toHaveValue('1');
  });

  test('the grid views still work alongside the tray', async ({ page, request }) => {
    // The tray short-circuits the grid loader, so switching back has to
    // actually re-fetch rather than show an empty table.
    const org = await createOrg(request, 'tray-switch');
    await request.post('/v1/candidates', {
      headers: org.headers,
      data: { full_name: 'Switch Person', source: 'manual', source_id: 'switch' },
    });

    await login(page, org.email);
    await page.goto('/contacts?view=discovered');
    await expect(page.getByTestId('discovered-tray')).toBeVisible();

    await page.getByTestId('view-tabs').getByRole('link', { name: 'Contacts' }).click();
    await expect(page.getByTestId('contacts-grid')).toBeVisible();
    await expect(page.getByTestId('contact-row')).toHaveCount(1);
  });
});
