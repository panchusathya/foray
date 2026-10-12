import fs from 'node:fs';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

/**
 * The Deliverability page (Settings → Deliverability).
 *
 * Like mailboxes.spec.js, a browser cannot fabricate a connected mailbox, and
 * it cannot stand up a real IMAP seed either -- so this covers what is
 * reachable from a fresh org: the way in from Settings, the empty states, a
 * seed that cannot sign in being refused at the form (and not saved, and its
 * password never shown), the provider-specific form, and the schedule
 * surviving a reload. The populated cases -- seed placements, the per-domain
 * rollup, health labels, block bounces -- are driven through the API in
 * packages/core/tests/test_deliverability_*.py, which can build mailboxes.
 *
 * Artifact: test-results/deliverability-artifact/ holds the empty page, the
 * refused seed with its error, and the page after the schedule change.
 */
const ARTIFACT_DIR = path.join('test-results', 'deliverability-artifact');

test.describe('deliverability', () => {
  test.beforeAll(() => fs.mkdirSync(ARTIFACT_DIR, { recursive: true }));

  test('settings leads to an honest empty page', async ({ page, request }) => {
    const org = await createOrg(request, 'deliv-empty');
    await login(page, org.email);
    await page.goto('/settings');
    await page.getByTestId('deliverability-link').click();

    await expect(page).toHaveURL(/\/deliverability$/);
    await expect(page.getByRole('heading', { name: 'Deliverability' })).toBeVisible();
    await expect(page.getByText('Measurement only', { exact: false })).toBeVisible();
    await expect(page.getByTestId('seed-empty')).toBeVisible();
    await expect(page.getByTestId('no-mailboxes')).toBeVisible();
    // Nothing to show yet means no recent-tests table, not an empty one.
    await expect(page.getByTestId('recent-tests')).toHaveCount(0);
    await page.screenshot({ path: path.join(ARTIFACT_DIR, '01-empty.png'), fullPage: true });
  });

  test('the sidebar has it, and the old Settings address still lands there', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'deliv-nav');
    await login(page, org.email);
    await page.goto('/');

    const sending = page.locator('.sidebar .sidebar-section', { hasText: 'SENDING' });
    const link = sending.getByRole('link', { name: 'Deliverability', exact: true });
    await expect(link).toBeVisible();
    await link.click();
    await expect(page).toHaveURL(/\/deliverability$/);
    await expect(link).toHaveClass(/active/);
    await expect(page.getByTestId('seed-card')).toBeVisible();
    // Its own destination, not a Settings sub-page: Settings must not light up.
    await expect(
      page.locator('.sidebar-account').getByRole('link', { name: 'Settings' }),
    ).not.toHaveClass(/active/);
    await page.screenshot({ path: path.join(ARTIFACT_DIR, '00-sidebar.png') });

    await page.goto('/settings/deliverability');
    await expect(page).toHaveURL(/\/deliverability$/);
    await expect(page.getByRole('heading', { name: 'Deliverability' })).toBeVisible();
  });

  test('a seed that cannot sign in is refused and not saved', async ({ page, request }) => {
    const org = await createOrg(request, 'deliv-seed');
    await login(page, org.email);
    await page.goto('/deliverability');

    // Gmail first: the form explains app passwords, and asks for no host.
    await expect(page.getByTestId('seed-form')).toContainText('app password');
    await expect(page.getByTestId('seed-host')).toHaveCount(0);

    // Other IMAP needs a host; point it at a port nothing listens on.
    await page.getByTestId('seed-provider').selectOption('other');
    await expect(page.getByTestId('seed-host')).toBeVisible();
    const secret = 'not-a-real-password-4471';
    await page.getByTestId('seed-address').fill('seed-probe@e2e.example.com');
    await page.getByTestId('seed-password').fill(secret);
    await page.getByTestId('seed-host').fill('127.0.0.1');
    await page.getByLabel('IMAP port').fill('1');
    await page.getByTestId('seed-submit').click();

    const error = page.getByTestId('seed-form-error');
    await expect(error).toBeVisible();
    await expect(error).toContainText('Could not reach 127.0.0.1:1');
    await expect(page.locator('body')).not.toContainText(secret);
    await page.screenshot({ path: path.join(ARTIFACT_DIR, '02-refused-seed.png'), fullPage: true });

    // Not saved: a reload still shows no seeds, and the API agrees.
    await page.reload();
    await expect(page.getByTestId('seed-empty')).toBeVisible();
    const overview = await request.get('/v1/deliverability', { headers: org.headers });
    expect((await overview.json()).seeds).toEqual([]);
  });

  test('seeds can be connected with Google or Yahoo sign-in instead of an app password', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'deliv-oauth');
    await login(page, org.email);
    await page.goto('/deliverability');

    const google = page.getByTestId('seed-oauth-google');
    const yahoo = page.getByTestId('seed-oauth-yahoo');
    await expect(google).toBeVisible();
    await expect(yahoo).toBeVisible();
    await expect(page.getByTestId('seed-oauth')).toContainText('no app password needed');
    // A dev / staging stack without provider credentials: the buttons are
    // there, disabled, and say which settings would switch them on.
    if (await yahoo.isDisabled()) {
      await expect(page.locator('code', { hasText: 'YAHOO_CLIENT_ID' })).toBeVisible();
      await expect(page.locator('code', { hasText: '/v1/deliverability/seeds/yahoo/callback' })).toBeVisible();
    }
    if (await google.isDisabled()) {
      await expect(page.locator('code', { hasText: 'GMAIL_CLIENT_ID' })).toBeVisible();
    }
    await page.getByTestId('seed-card').screenshot({
      path: path.join(ARTIFACT_DIR, '05-seed-oauth.png'),
    });
  });

  test('the seed test schedule is saved', async ({ page, request }) => {
    const org = await createOrg(request, 'deliv-schedule');
    await login(page, org.email);
    await page.goto('/deliverability');

    const select = page.getByTestId('interval-select');
    await expect(select).toHaveValue('24');
    await select.selectOption('48');
    // The select is disabled while saving; wait for it to settle, then reload.
    await expect(select).toBeEnabled();
    await page.reload();
    await expect(page.getByTestId('interval-select')).toHaveValue('48');
    await page.screenshot({ path: path.join(ARTIFACT_DIR, '03-schedule-saved.png'), fullPage: true });
  });
});
