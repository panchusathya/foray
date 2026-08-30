import { expect, test } from '@playwright/test';

import {
  addToProject,
  createOrg,
  createProject,
  createSequence,
  login,
  seedCandidates,
} from './support.js';

/**
 * Contacts, narrowed to the project in the sidebar.
 *
 * The question this exists for is "who answered the robot role's outreach" —
 * org-wide, the relationship views answer a question nobody asked. The
 * dangerous failure is the quiet one: a grid that is filtered without saying
 * so reads as "you have nobody", so the scope has to be visible, switchable,
 * and carried across the tabs.
 *
 * The reply half of the story is asserted in
 * `packages/core/tests/test_crm_grid.py::TestProjectScope` — an inbound
 * message needs a provider to ingest it, which a browser cannot stage. What
 * is asserted here is the part that only exists in the browser.
 */
test.describe('contacts project scope', () => {
  /**
   * Two mandates, one person on each, and a third in neither.
   *
   * Ada is only ever *enrolled* in the robot role — no pipeline card — because
   * that is the case a membership-only filter gets wrong, and she is exactly
   * the person you are waiting on.
   */
  async function twoMandates(request, tag) {
    const org = await createOrg(request, tag);
    const [ada, grace] = await seedCandidates(request, org, tag);

    // Created first, so the newest-first project list leaves Robot selected.
    const other = await createProject(request, org, `${tag} Data Role`);
    await addToProject(request, org, other.id, [grace]);

    const robot = await createProject(request, org, `${tag} Robot Role`);
    const sequence = await createSequence(request, org, robot.id);
    await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: [ada], project_id: robot.id },
    });
    await request.post(`/v1/sequences/${sequence.id}/run`, { headers: org.headers });

    return { org, robot, other, tag };
  }

  const names = (page) => page.getByTestId('contact-row').locator('td[data-col="full_name"] a');

  test('opens on the sidebar project and says so', async ({ page, request }) => {
    const { org, tag } = await twoMandates(request, 'scope-default');

    await login(page, org.email);
    await page.goto('/contacts');

    // Enrolled in the robot role and on nobody's board: the person a
    // pipeline-only filter would have dropped.
    await expect(names(page)).toHaveText([`Ada ${tag}`]);
    // A count nobody can attribute to a population is not a fact.
    await expect(page.locator('.page-header')).toContainText('Robot Role');
    await expect(page.getByTestId('scope-project')).toHaveAttribute('aria-pressed', 'true');
  });

  test('switching the sidebar project re-scopes the grid', async ({ page, request }) => {
    const { org, tag } = await twoMandates(request, 'scope-switch');

    await login(page, org.email);
    await page.goto('/contacts');
    await expect(names(page)).toHaveText([`Ada ${tag}`]);

    await page.locator('#project-switcher').selectOption({ label: `${tag} Data Role` });
    await expect(names(page)).toHaveText([`Grace ${tag}`]);
  });

  test('all contacts is one click away and is a real escape hatch', async ({ page, request }) => {
    const { org, tag } = await twoMandates(request, 'scope-all');

    await login(page, org.email);
    await page.goto('/contacts');

    await page.getByTestId('scope-all').click();
    await expect(page).toHaveURL(/project=all/);
    await expect(names(page)).toHaveText([`Ada ${tag}`, `Grace ${tag}`]);
    await expect(page.locator('.page-header')).not.toContainText('Robot Role');
  });

  test('the scope survives a tab switch', async ({ page, request }) => {
    const { org, tag } = await twoMandates(request, 'scope-tabs');

    await login(page, org.email);
    await page.goto('/contacts');
    await page.getByTestId('scope-all').click();
    await expect(names(page)).toHaveCount(2);

    // Re-picking the population on every tab is the friction this removes.
    await page.getByTestId('view-tabs').getByRole('link', { name: 'Recently added' }).click();
    await expect(page).toHaveURL(/view=recent/);
    await expect(page).toHaveURL(/project=all/);
    await expect(names(page)).toHaveCount(2);
    await expect(page.getByTestId('scope-all')).toHaveAttribute('aria-pressed', 'true');
  });

  test('an empty scoped grid says it is filtered rather than empty', async ({ page, request }) => {
    const { org, tag } = await twoMandates(request, 'scope-empty');

    await login(page, org.email);
    // Grace is on the data role, so searching for her inside the robot role
    // finds nothing -- and must not imply she is not in the CRM at all.
    await page.goto(`/contacts?q=Grace ${tag}`);

    const escape = page.getByTestId('empty-scope-all');
    await expect(escape).toBeVisible();
    await escape.click();
    await expect(names(page)).toHaveText([`Grace ${tag}`]);
  });

  test('the workspace-wide views ignore the scope entirely', async ({ page, request }) => {
    const { org, tag } = await twoMandates(request, 'scope-pool');

    await login(page, org.email);
    // "Every profile in the workspace" has to mean it, so there is nothing
    // to scope by and no control offering to.
    await page.goto('/contacts?view=all');

    await expect(page.getByTestId('project-scope')).toHaveCount(0);
    await expect(names(page)).toHaveCount(3);
    await expect(names(page)).toContainText([`Ada ${tag}`, `Grace ${tag}`, `Noemail ${tag}`]);
  });
});
