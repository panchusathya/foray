import { expect, test } from '@playwright/test';

import { createOrg, createProject, createSequence, login, seedCandidates } from './support.js';

/**
 * Placeholders the renderer cannot read, in the sequence editor.
 *
 * `{{First name}}` — a space is not a legal variable character — matched none
 * of the template patterns, so nothing substituted it and nothing warned about
 * it, and an email went out reading "Hi {{First name}},". The contract covered
 * here is the pair that stops that: the editor names the placeholder while the
 * step is still on screen, and the preview shows the raw braces the candidate
 * would have seen rather than quietly rendering something else.
 */
test.describe('malformed placeholders in the sequence editor', () => {
  test('the editor names a broken placeholder and the preview shows what would send', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'malformed');
    await seedCandidates(request, org, 'malformed');
    const project = await createProject(request, org, 'Malformed Role');
    const sequence = await createSequence(request, org, project.id, {
      steps: [
        {
          delay_days: 0,
          subject_template: 'Quick note for {{ first_name }}',
          body_template: 'Hi {{First name}},\n\nBest,\nAnna',
        },
      ],
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    const warning = page.getByTestId('malformed-placeholders');
    await expect(warning).toBeVisible();
    await expect(warning).toContainText('Step 1');
    await expect(warning).toContainText('{{First name}}');

    await page.getByRole('button', { name: 'Preview' }).click();
    // The braces are the honest preview: that is what the candidate would read.
    await expect(page.getByTestId('preview-body')).toContainText('{{First name}}');
    await expect(page.getByTestId('preview-warnings')).toContainText('{{First name}}');
  });

  test('fixing the placeholder clears the warning without a save', async ({ page, request }) => {
    const org = await createOrg(request, 'malformed-fix');
    await seedCandidates(request, org, 'malformed-fix');
    const project = await createProject(request, org, 'Malformed Fix Role');
    const sequence = await createSequence(request, org, project.id, {
      steps: [
        {
          delay_days: 0,
          subject_template: 'Quick note for {{ first_name }}',
          body_template: 'Hi {{First name}},\n\nBest,\nAnna',
        },
      ],
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);
    await expect(page.getByTestId('malformed-placeholders')).toBeVisible();

    const body = page.getByTestId('email-body-editor');
    await body.click();
    await page.keyboard.press('Control+A');
    await page.keyboard.type('Hi {{ first_name | there }},');

    await expect(page.getByTestId('malformed-placeholders')).toHaveCount(0);
  });

  test('a clean sequence shows no warning at all', async ({ page, request }) => {
    const org = await createOrg(request, 'malformed-clean');
    await seedCandidates(request, org, 'malformed-clean');
    const project = await createProject(request, org, 'Clean Role');
    const sequence = await createSequence(request, org, project.id);

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    await expect(page.getByTestId('malformed-placeholders')).toHaveCount(0);
    await page.getByRole('button', { name: 'Preview' }).click();
    await expect(page.getByTestId('preview-warnings')).toHaveCount(0);
  });
});
