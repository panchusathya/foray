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
 * The Contacts grid: preset views, sorting, and URL state.
 *
 * The views decide who a recruiter is shown when they ask "who is going
 * cold". A wrong filter is not a visible bug — it is a person quietly never
 * contacted again — so each preset is asserted against a seeded population
 * with a known history rather than against whatever happens to be there.
 */
test.describe('contacts grid', () => {
  /** Seeds an org where candidate[0] has been emailed and the rest have not. */
  async function population(request, tag) {
    const org = await createOrg(request, tag);
    const ids = await seedCandidates(request, org, tag);
    const project = await createProject(request, org, `${tag} Role`);
    await addToProject(request, org, project.id, ids);
    const sequence = await createSequence(request, org, project.id);
    await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: [ids[0]], project_id: project.id },
    });
    await request.post(`/v1/sequences/${sequence.id}/run`, { headers: org.headers });
    return { org, ids };
  }

  test('renders a sortable grid with the expected columns', async ({ page, request }) => {
    const { org } = await population(request, 'grid-cols');

    await login(page, org.email);
    await page.goto('/contacts');

    await expect(page.getByTestId('contacts-grid')).toBeVisible();
    for (const header of ['Name', 'Title', 'Company', 'Last contacted', 'Last reply', 'Touches']) {
      await expect(page.getByRole('columnheader', { name: new RegExp(header) })).toBeVisible();
    }
    await expect(page.getByTestId('contact-row').first()).toBeVisible();
  });

  test('sorting flips aria-sort and stays in the URL', async ({ page, request }) => {
    const { org } = await population(request, 'grid-sort');

    await login(page, org.email);
    await page.goto('/contacts');

    await page.getByTestId('sort-last_contacted').click();
    await expect(page).toHaveURL(/sort=last_contacted/);
    const header = page.getByRole('columnheader', { name: /Last contacted/ });
    await expect(header).toHaveAttribute('aria-sort', 'ascending');

    await page.getByTestId('sort-last_contacted').click();
    await expect(header).toHaveAttribute('aria-sort', 'descending');
    await expect(page).toHaveURL(/dir=desc/);
  });

  test('the going-cold view excludes someone contacted today', async ({ page, request }) => {
    // The candidate was emailed moments ago by the sequence run, so nothing
    // qualifies as cold. An empty state here is the correct answer.
    const { org } = await population(request, 'grid-cold');

    await login(page, org.email);
    await page.goto('/contacts?view=going_cold');

    await expect(page.getByTestId('view-tabs')).toBeVisible();
    await expect(page.getByTestId('contact-row')).toHaveCount(0);
  });

  test('the sourced pool shows people the CRM view hides', async ({ page, request }) => {
    // Uncontacted, unshortlisted people are in the pool but not the CRM.
    const org = await createOrg(request, 'grid-pool');
    await seedCandidates(request, org, 'grid-pool');

    await login(page, org.email);

    // Positive assertion on the empty state: toHaveCount(0) would also pass
    // against a grid that simply had not loaded yet.
    await page.goto('/contacts?view=crm');
    await expect(page.getByText('No one in the CRM yet', { exact: false })).toBeVisible();

    await page.goto('/contacts?view=all');
    await expect(page.getByTestId('contacts-grid')).toBeVisible();
    expect(await page.getByTestId('contact-row').count()).toBeGreaterThan(0);
  });

  test('the active view survives a reload and the back button', async ({ page, request }) => {
    const { org } = await population(request, 'grid-url');

    await login(page, org.email);
    await page.goto('/contacts?view=crm');
    await page.getByRole('link', { name: 'Replied' }).click();
    await expect(page).toHaveURL(/view=replied/);

    await page.reload();
    await expect(page).toHaveURL(/view=replied/);

    await page.goBack();
    await expect(page).toHaveURL(/view=crm/);
  });

  test('search narrows the grid and clears back to the full view', async ({ page, request }) => {
    const { org } = await population(request, 'grid-search');

    await login(page, org.email);
    await page.goto('/contacts?view=all');
    await expect(page.getByTestId('contacts-grid')).toBeVisible();
    const before = await page.getByTestId('contact-row').count();

    await page.getByTestId('contacts-search').fill('Ada grid-search');
    await expect(page).toHaveURL(/q=Ada/);
    await expect(page.getByTestId('contact-row')).toHaveCount(1);

    await page.getByTestId('contacts-search').fill('');
    await expect(page.getByTestId('contact-row')).toHaveCount(before);
  });
});
