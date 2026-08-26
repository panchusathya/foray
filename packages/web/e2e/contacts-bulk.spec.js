import { expect, test } from '@playwright/test';

import { createOrg, createProject, createSequence, login, seedCandidates } from './support.js';

/**
 * Bulk actions on the Contacts grid.
 *
 * These act on real people — adding them to a pipeline, or starting outreach
 * to them. The cases that matter are the ones where the UI could quietly do
 * something other than what the recruiter thinks it is doing: a selection that
 * carries no rows, or an enrolment that silently drops someone with no email.
 */
test.describe('contacts bulk actions', () => {
  async function workspace(request, tag) {
    const org = await createOrg(request, tag);
    const ids = await seedCandidates(request, org, tag);
    const project = await createProject(request, org, `${tag} Role`);
    const sequence = await createSequence(request, org, project.id);
    return { org, ids, project, sequence };
  }

  test('the bar stays hidden until something is selected', async ({ page, request }) => {
    const { org } = await workspace(request, 'bulk-hidden');

    await login(page, org.email);
    await page.goto('/contacts?view=all');
    await expect(page.getByTestId('contacts-grid')).toBeVisible();
    await expect(page.getByTestId('bulk-bar')).toHaveCount(0);

    await page.getByTestId('contact-row').first().locator('input[type=checkbox]').click();
    await expect(page.getByTestId('bulk-bar')).toBeVisible();
    await expect(page.getByTestId('bulk-count')).toHaveText('1 selected');
  });

  test('select page selects every row, and clear empties it', async ({ page, request }) => {
    const { org } = await workspace(request, 'bulk-page');

    await login(page, org.email);
    await page.goto('/contacts?view=all');
    await expect(page.getByTestId('contacts-grid')).toBeVisible();
    const rows = await page.getByTestId('contact-row').count();

    await page.getByTestId('select-page').click();
    await expect(page.getByTestId('bulk-count')).toHaveText(`${rows} selected`);

    await page.getByRole('button', { name: 'Clear' }).click();
    await expect(page.getByTestId('bulk-bar')).toHaveCount(0);
  });

  test('adding to a project reports the outcome and survives the clear', async ({
    page,
    request,
  }) => {
    // The confirmation used to live inside the bar, which unmounts the instant
    // the selection clears — so a successful action left no trace at all.
    const { org, project } = await workspace(request, 'bulk-project');

    await login(page, org.email);
    await page.goto('/contacts?view=all');
    await expect(page.getByTestId('contacts-grid')).toBeVisible();

    await page.getByTestId('select-page').click();
    await page.getByTestId('bulk-bar').getByRole('combobox').first().selectOption({ index: 1 });
    await page.getByTestId('bulk-add-to-project').click();

    await expect(page.getByTestId('bulk-result')).toContainText('Added');
    await expect(page.getByTestId('bulk-bar')).toHaveCount(0);

    // And it really landed, not just said so.
    await page.goto(`/projects/${project.id}`);
    await expect(page.getByRole('link', { name: /bulk-project/ }).first()).toBeVisible();
  });

  test('enrolling warns about people with no email', async ({ page, request }) => {
    const { org } = await workspace(request, 'bulk-noemail');
    await request.post('/v1/candidates', {
      headers: org.headers,
      data: { full_name: 'Anon bulk-noemail', source: 'manual', source_id: 'anon-bulk-noemail' },
    });

    await login(page, org.email);
    await page.goto('/contacts?view=all');
    await expect(page.getByTestId('contacts-grid')).toBeVisible();

    let asked = '';
    page.on('dialog', (dialog) => {
      asked = dialog.message();
      dialog.dismiss();
    });

    await page.getByTestId('select-page').click();
    await page.getByTestId('bulk-bar').getByRole('combobox').nth(1).selectOption({ index: 1 });
    await page.getByTestId('bulk-enrol').click();

    // Same wording as the sequence editor's enrol panel, which says enrolling
    // them stops permanently rather than that they wait for an address.
    await expect.poll(() => asked).toContain('have no email');
    // Dismissed, so nothing ran.
    await expect(page.getByTestId('bulk-result')).toHaveCount(0);
    await expect(page.getByTestId('bulk-bar')).toBeVisible();
  });

  test('select-all-matching cannot be exported, because it has no columns', async ({
    page,
    request,
  }) => {
    const { org } = await workspace(request, 'bulk-idsonly');

    await login(page, org.email);
    await page.goto('/contacts?view=all');
    await expect(page.getByTestId('contacts-grid')).toBeVisible();

    await page.getByTestId('contact-row').first().locator('input[type=checkbox]').click();
    await expect(page.getByTestId('bulk-export')).toBeEnabled();

    await page.getByTestId('select-all-matching').click();
    await expect(page.getByTestId('bulk-export')).toBeDisabled();
  });

  test('changing view drops a selection you can no longer see', async ({ page, request }) => {
    const { org } = await workspace(request, 'bulk-viewswap');

    await login(page, org.email);
    await page.goto('/contacts?view=all');
    await expect(page.getByTestId('contacts-grid')).toBeVisible();
    await page.getByTestId('select-page').click();
    await expect(page.getByTestId('bulk-bar')).toBeVisible();

    await page.getByRole('link', { name: 'Replied' }).click();
    await expect(page.getByTestId('bulk-bar')).toHaveCount(0);
  });
});
