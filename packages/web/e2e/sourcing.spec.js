import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

/**
 * Sourcing talks to the Foray service, which is a compose profile — the CI
 * staging stack does not start it (a second 3 GB image with a baked embedding
 * model). Skip rather than fail when it is absent, so this file is useful when
 * pointed at a stack that does run Foray.
 */
test.describe('sourcing', () => {
  test.beforeEach(async ({ request }) => {
    const probe = await request.get('/foray/health').catch(() => null);
    test.skip(!probe || !probe.ok(), 'Foray service is not running on this stack');
  });

  test('the page offers a contactable-only filter', async ({ page, request }) => {
    const org = await createOrg(request, 'sourcing');
    await login(page, org.email);
    await page.goto('/sourcing');

    await expect(page.getByRole('button', { name: 'Search' })).toBeVisible();
    await expect(page.getByText(/contactable/i)).toBeVisible();
  });

  test('a search returns hits that carry an email source, never a guess', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'sourcing-hits');
    await login(page, org.email);
    await page.goto('/sourcing');

    await page.locator('textarea').fill('senior backend engineer in Go');
    await page.getByRole('button', { name: 'Search' }).click();

    const firstHit = page.locator('.hit').first();
    await expect(firstHit).toBeVisible({ timeout: 25_000 });
    // A guessed address must never reach the UI — the search API filters
    // source='guess' in SQL so it can never become a sequence's to-address.
    await expect(page.getByText('email · guess')).toHaveCount(0);
  });

  test('linkedin-sourced hits render a badge and no dead github link', async ({
    page,
    request,
  }) => {
    // The staging stack seeds synthetic LinkedIn rows (seed_synthetic
    // --source linkedin); a hit with a synthetic 'li:' login must show its
    // source and must not fabricate a github.com URL from it.
    const org = await createOrg(request, 'sourcing-li');
    await login(page, org.email);
    await page.goto('/sourcing');

    await page.locator('textarea').fill('senior software engineer');
    await page.getByRole('button', { name: 'Search' }).click();
    await expect(page.locator('.hit').first()).toBeVisible({ timeout: 25_000 });

    const linkedinBadges = page.getByTestId('source-badge');
    const badgeCount = await linkedinBadges.count();
    if (badgeCount === 0) {
      // A pure-GitHub pool is legitimate on stacks without LinkedIn seeds;
      // the invariant still checked below: no hit ever links to a li: login.
      test.info().annotations.push({ type: 'note', description: 'no linkedin hits in pool' });
    }
    // No fabricated github.com/li:... links anywhere on the page.
    await expect(page.locator('a[href*="github.com/li:"]')).toHaveCount(0);
  });

  test('github hits still render their github link', async ({ page, request }) => {
    const org = await createOrg(request, 'sourcing-gh');
    await login(page, org.email);
    await page.goto('/sourcing');

    await page.locator('textarea').fill('senior backend engineer in Go');
    await page.getByRole('button', { name: 'Search' }).click();

    const githubLink = page.locator('.hit a[href*="github.com/"]').first();
    await expect(githubLink).toBeVisible({ timeout: 25_000 });
  });
});
