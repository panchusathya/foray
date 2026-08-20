import { expect, test } from '@playwright/test';

import { createOrg, createProject, createSequence, login } from './support.js';

/**
 * Sending mailboxes in the UI.
 *
 * A browser cannot complete either provider's OAuth round trip, and there is
 * no endpoint that fabricates a connection (by design — a mailbox exists only
 * because somebody consented to it). So these cover the states reachable
 * without one: which providers are offered, and what the sequence editor says
 * when the pool is empty. The populated cases — caps, usage, rotation spread —
 * are covered in packages/core/tests/test_rotation.py, which can build the
 * pool directly.
 */
test.describe('sending mailboxes', () => {
  test('settings offers every provider the server knows about', async ({ page, request }) => {
    const org = await createOrg(request, 'mailbox');
    await login(page, org.email);
    await page.goto('/settings');

    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
    // Both providers are listed whether or not this deployment has credentials
    // for them, so "why can't I add Zoho" has a visible answer.
    await expect(page.getByRole('button', { name: /Connect Outlook/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Connect Zoho Mail/ })).toBeVisible();
    await expect(page.getByText('No mailbox connected yet.')).toBeVisible();
  });

  test('an unconfigured provider says which env vars it needs', async ({ page, request }) => {
    const org = await createOrg(request, 'mailbox-cfg');
    await login(page, org.email);
    await page.goto('/settings');

    // The staging stack deliberately withholds provider credentials, so the
    // buttons are present but disabled and the reason is on screen.
    const outlook = page.getByRole('button', { name: /Connect Outlook/ });
    if (await outlook.isDisabled()) {
      await expect(page.getByText(/needs/).first()).toBeVisible();
      await expect(page.locator('code', { hasText: 'MS_CLIENT_ID' })).toBeVisible();
      await expect(page.locator('code', { hasText: 'ZOHO_CLIENT_ID' })).toBeVisible();
    }
  });

  test('the sequence editor picks a sender at sequence level, not per step', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'sender');
    const project = await createProject(request, org, 'Sender Role');
    const sequence = await createSequence(request, org, project.id);
    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    const picker = page.getByTestId('sender-picker');
    await expect(picker).toBeVisible();
    // Rotation is the default, and with nothing connected the page says how to
    // fix that rather than silently falling back.
    await expect(picker).toHaveValue('rotate');
    await expect(page.getByRole('link', { name: 'Connect a mailbox' })).toBeVisible();

    // Sequence-level: it sits outside the step editor, so switching steps
    // cannot change it. Adding a step must leave the picker where it was.
    await page.getByRole('button', { name: '+ Add step' }).click();
    await expect(page.getByTestId('sender-picker')).toHaveCount(1);
    await expect(page.getByTestId('sender-picker')).toHaveValue('rotate');
  });
});
