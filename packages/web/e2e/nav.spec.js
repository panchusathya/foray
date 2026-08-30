import { expect, test } from '@playwright/test';

import { createOrg, createProject, login } from './support.js';

/**
 * The sidebar.
 *
 * It is grouped rather than flat because the app contains two different jobs —
 * filling a role for a client, and placing a person we represent — and a flat
 * list interleaved them. The grouping is therefore load-bearing, not
 * decoration: what is asserted here is that each section holds the pages that
 * actually belong to it, and that the Project group only ever claims pages the
 * picker really governs.
 */
test.describe('sidebar', () => {
  const sidebar = (page) => page.locator('.sidebar');
  const groupFor = (page, heading) =>
    sidebar(page).locator('.sidebar-section', { hasText: heading });

  test('groups every destination under the question it answers', async ({ page, request }) => {
    const org = await createOrg(request, 'nav-groups');
    await createProject(request, org, 'Robot Role');

    await login(page, org.email);
    await page.goto('/');

    for (const [heading, labels] of [
      ['PROJECT', ['Pipeline', 'Contacts', 'Sequences']],
      ['FIND PEOPLE', ['Search', 'Sourcing', 'Import']],
      ['PLACE PEOPLE', ['Intake', 'Jobs', 'Applications']],
    ]) {
      const group = groupFor(page, heading);
      await expect(group).toBeVisible();
      for (const label of labels) {
        await expect(group.getByRole('link', { name: label, exact: true })).toBeVisible();
      }
    }

    // Home and the account controls sit outside the groups on purpose.
    await expect(sidebar(page).getByRole('link', { name: 'Home', exact: true })).toBeVisible();
    await expect(page.locator('.sidebar-account').getByRole('link', { name: 'Settings' })).toBeVisible();
  });

  test('Pipeline points at the selected project and follows the picker', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'nav-pipeline');
    const first = await createProject(request, org, 'Data Role');
    // Newest first, so this is the one the picker opens on.
    const second = await createProject(request, org, 'Robot Role');

    await login(page, org.email);
    await page.goto('/');

    const pipeline = sidebar(page).getByRole('link', { name: 'Pipeline' });
    await expect(pipeline).toHaveAttribute('href', `/projects/${second.id}`);

    await page.locator('#project-switcher').selectOption({ label: 'Data Role' });
    await expect(pipeline).toHaveAttribute('href', `/projects/${first.id}`);
  });

  test('opening a board switches the picker to it', async ({ page, request }) => {
    // Otherwise the switcher names one project while the board shows another,
    // which with Pipeline sitting under the picker is a flat contradiction.
    const org = await createOrg(request, 'nav-adopt');
    const first = await createProject(request, org, 'Data Role');
    await createProject(request, org, 'Robot Role');

    await login(page, org.email);
    await page.goto(`/projects/${first.id}`);

    await expect(page.locator('#project-switcher')).toHaveValue(first.id);
    await expect(page.locator('.page-header')).toContainText('Data Role');
  });

  test('with no project, Pipeline gives up its slot and the rest stay', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'nav-noproject');

    await login(page, org.email);
    await page.goto('/');

    const group = groupFor(page, 'PROJECT');
    // Nowhere for it to point, so it is replaced rather than dead.
    await expect(group.getByRole('link', { name: 'Pipeline' })).toHaveCount(0);
    await expect(group.getByRole('link', { name: 'Create your first project' })).toBeVisible();
    // Both work unscoped, so neither disappears.
    await expect(group.getByRole('link', { name: 'Contacts' })).toBeVisible();
    await expect(group.getByRole('link', { name: 'Sequences' })).toBeVisible();
  });

  test('Analytics is not a destination', async ({ page, request }) => {
    const org = await createOrg(request, 'nav-noanalytics');

    await login(page, org.email);
    await page.goto('/');

    await expect(sidebar(page).getByRole('link', { name: 'Analytics' })).toHaveCount(0);
  });
});
