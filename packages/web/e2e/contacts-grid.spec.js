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
    for (const header of [
      'Name',
      'Title',
      'Company',
      'Last contacted',
      'Last reply',
      'Touches',
      'Status',
    ]) {
      await expect(page.getByRole('columnheader', { name: new RegExp(header) })).toBeVisible();
    }
    await expect(page.getByTestId('contact-row').first()).toBeVisible();
  });

  test('the name cell carries the address without widening the grid', async ({
    page,
    request,
  }) => {
    // The address used to need its own column. Folding it under the name is
    // only an improvement if the cell still truncates -- an un-ellipsised
    // address is exactly what pushes a fixed-layout table sideways.
    const { org } = await population(request, 'grid-identity');
    // Deliberately far too long for the column. A fixture-length address
    // fits either way, which would make the layout half of this test pass
    // with the fix removed.
    const longEmail = 'aaaaaaaaaa.bbbbbbbbbb.cccccccccc@a-very-long-company-domain.example.com';
    await request.post('/v1/candidates', {
      headers: org.headers,
      data: {
        source: 'e2e',
        source_id: 'grid-identity-long',
        full_name: 'Aaa Longmail',
        emails: [longEmail],
      },
    });

    await login(page, org.email);
    await page.goto('/contacts');

    const cell = page
      .getByTestId('contact-row')
      .filter({ hasText: 'Ada grid-identity' })
      .locator('td[data-col="full_name"]');
    // The link is the name alone -- specs find people by accessible name.
    await expect(cell.getByRole('link', { name: 'Ada grid-identity' })).toBeVisible();
    await expect(cell).toContainText('ada-grid-identity@e2e.example.com');
    // The contactless fixture says so rather than rendering a blank line.
    await expect(
      page
        .getByTestId('contact-row')
        .filter({ hasText: 'Noemail grid-identity' })
        .locator('td[data-col="full_name"]'),
    ).toContainText('no email');

    const layout = await page.evaluate(() => {
      const row = [...document.querySelectorAll('[data-testid="contact-row"]')].find((tr) =>
        tr.textContent.includes('Aaa Longmail'),
      );
      const td = row.querySelector('td[data-col="full_name"]');
      const sub = td.querySelector('.cell-sub');
      return {
        truncated: sub.scrollWidth > sub.clientWidth,
        addressFits: sub.getBoundingClientRect().right <= td.getBoundingClientRect().right + 1,
        gridWidth: document.querySelector('.dgrid').scrollWidth,
        pageScrollsSideways:
          document.documentElement.scrollWidth > document.documentElement.clientWidth,
      };
    });
    // Ellipsised inside its cell rather than stretching the fixed layout.
    expect(layout.truncated).toBe(true);
    expect(layout.addressFits).toBe(true);
    expect(layout.gridWidth).toBeLessThan(1200);
    expect(layout.pageScrollsSideways).toBe(false);
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
