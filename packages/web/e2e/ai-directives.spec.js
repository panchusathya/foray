import { expect, test } from '@playwright/test';

import { createOrg, createProject, createSequence, login, seedCandidates } from './support.js';

/**
 * {{ai: ...}} directives in the editor preview. CI runs with the model off
 * (JUICEBOX_LLM_ENABLED=false), so the covered contract is the fail-safe one:
 * the preview must show a visible [AI: ...] marker and a banner — never a 500,
 * never a blank body, and never the raw {{ai: ...}} syntax.
 */
test.describe('AI directives in the sequence editor', () => {
  test('a directive step previews with a marker and a banner when no model is up', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'aidir');
    await seedCandidates(request, org, 'aidir');
    const project = await createProject(request, org, 'AI Dir Role');
    const sequence = await createSequence(request, org, project.id, {
      steps: [
        {
          delay_days: 0,
          subject_template: 'Quick note for {{first_name}}',
          body_template:
            'Hi {{first_name}},\n\n{{ai: say why this role is a fit}}\n\nWorth a chat?',
        },
      ],
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);
    await page.getByRole('button', { name: 'Preview' }).click();

    const body = page.getByTestId('preview-body');
    await expect(body).toContainText('[AI: say why this role is a fit]');
    await expect(body).not.toContainText('{{ai:');
    await expect(page.getByTestId('ai-unavailable-banner')).toBeVisible();
  });

  test('a plain step previews without any AI banner', async ({ page, request }) => {
    const org = await createOrg(request, 'aidir-plain');
    await seedCandidates(request, org, 'aidir-plain');
    const project = await createProject(request, org, 'Plain Role');
    const sequence = await createSequence(request, org, project.id);

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);
    await page.getByRole('button', { name: 'Preview' }).click();

    await expect(page.getByTestId('preview-body')).toContainText('Hi');
    await expect(page.getByTestId('ai-unavailable-banner')).toHaveCount(0);
  });

  test('the ai chip inserts a directive skeleton into the body', async ({ page, request }) => {
    const org = await createOrg(request, 'aidir-chip');
    await seedCandidates(request, org, 'aidir-chip');
    const project = await createProject(request, org, 'Chip Role');
    const sequence = await createSequence(request, org, project.id);

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    await page.getByPlaceholder('Body — use variables for anything candidate-specific').click();
    await page.getByRole('button', { name: 'ai: …' }).click();

    await expect(
      page.getByPlaceholder('Body — use variables for anything candidate-specific'),
    ).toHaveValue(/\{\{ai: say why this role is a fit\}\}/);
  });
});
