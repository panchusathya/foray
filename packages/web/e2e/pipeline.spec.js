import { expect, test } from '@playwright/test';

import { addToProject, createOrg, createProject, login, seedCandidates } from './support.js';

test.describe('search → project pipeline', () => {
  test('adding the same people twice does not duplicate the pipeline card', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'dupe');
    const ids = await seedCandidates(request, org, 'dupe');
    const project = await createProject(request, org, 'Dupe Guard');

    await addToProject(request, org, project.id, ids);
    // The second add is the scenario a user creates by re-running a search and
    // adding the same results again, or by double-clicking Add.
    await addToProject(request, org, project.id, ids);

    await login(page, org.email);
    await page.goto(`/projects/${project.id}`);

    for (const name of ['Ada dupe', 'Grace dupe', 'Noemail dupe']) {
      await expect(
        page.locator('.pipeline-card').filter({ hasText: name }),
        `${name} should appear exactly once`,
      ).toHaveCount(1);
    }
    await expect(page.locator('.pipeline-card')).toHaveCount(3);
  });

  test('a candidate can sit in two different projects at once', async ({ page, request }) => {
    const org = await createOrg(request, 'twoproj');
    const ids = await seedCandidates(request, org, 'twoproj');
    const first = await createProject(request, org, 'Backend Role');
    const second = await createProject(request, org, 'Platform Role');

    await addToProject(request, org, first.id, [ids[0]]);
    await addToProject(request, org, second.id, [ids[0]]);

    await login(page, org.email);
    await page.goto(`/projects/${first.id}`);
    await expect(page.locator('.pipeline-card').filter({ hasText: 'Ada twoproj' })).toHaveCount(1);
    await page.goto(`/projects/${second.id}`);
    await expect(page.locator('.pipeline-card').filter({ hasText: 'Ada twoproj' })).toHaveCount(1);
  });

  test('the board is a tracker: it offers no enrolment controls', async ({ page, request }) => {
    // Enrolment lives in the sequence editor. This guards the boundary — a
    // stray "enrol" control reappearing on the board is a regression.
    const org = await createOrg(request, 'tracker');
    const ids = await seedCandidates(request, org, 'tracker');
    const project = await createProject(request, org, 'Tracker Role');
    await addToProject(request, org, project.id, ids);

    await login(page, org.email);
    await page.goto(`/projects/${project.id}`);

    await expect(page.locator('.pipeline-card').first()).toBeVisible();
    await expect(page.getByRole('button', { name: /enrol/i })).toHaveCount(0);
  });

  test('outreach-driven stages are not plain one-click moves', async ({ page, request }) => {
    // "contacted" and "replied" must come from a real send or reply; the board
    // only offers them behind an explicit "I reached out" confirmation.
    const org = await createOrg(request, 'stages');
    const ids = await seedCandidates(request, org, 'stages');
    const project = await createProject(request, org, 'Stage Role');
    await addToProject(request, org, project.id, ids);

    await login(page, org.email);
    await page.goto(`/projects/${project.id}`);

    await expect(page.getByRole('button', { name: '→ contacted' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'I reached out →' }).first()).toBeVisible();
  });
});
