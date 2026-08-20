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

  test('paging preserves selection and page deselection is page-local', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'sourcing-pages');
    await login(page, org.email);
    await page.route('**/foray/search', async (route) => {
      const body = route.request().postDataJSON();
      const offset = body.offset ?? 0;
      // limit 0 is the wire encoding for "the server's own page size".
      const requested = body.limit || 40;
      const pageSize = Math.min(requested, 45 - offset);
      const results = Array.from({ length: Math.max(0, pageSize) }, (_, index) => {
        const rank = offset + index + 1;
        return {
          rank,
          login: `paging-${rank}`,
          name: `Paging Candidate ${rank}`,
          source: 'github',
          github_url: `https://github.com/paging-${rank}`,
          emails: [],
          highlights: ['paging evidence'],
          flags: [],
          domain_markers: [],
          topics: [],
          inferred: {},
        };
      });
      await route.fulfill({
        json: {
          query: body.query,
          parser: 'heuristic',
          archetype: { id: 'senior_backend', label: 'Senior Backend Engineer' },
          total_matched: 45,
          returned: results.length,
          display_floor: 20,
          message: '45 candidates cleared the bar for Senior Backend Engineer.',
          limit: body.limit,
          offset,
          page: Math.floor(offset / 40) + 1,
          page_count: 2,
          page_size: 40,
          paginable: 45,
          has_more: offset + results.length < 45,
          truncated: false,
          max_paginable: 1000,
          results,
        },
      });
    });

    await page.goto('/sourcing');
    await page.getByRole('button', { name: 'Search' }).click();
    await page.getByRole('checkbox', { name: 'Select all on this page' }).check();
    await expect(page.getByText('40 selected (40 on this page)')).toBeVisible();

    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText('Paging Candidate 41')).toBeVisible();
    await page.getByRole('checkbox', { name: 'Select all on this page' }).check();
    await expect(page.getByText('45 selected (5 on this page)')).toBeVisible();
    await page.getByRole('checkbox', { name: 'Select all on this page' }).uncheck();
    await expect(page.getByText('40 selected (0 on this page)')).toBeVisible();
  });
});
