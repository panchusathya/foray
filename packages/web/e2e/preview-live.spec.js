import fs from 'node:fs';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { addToProject, createOrg, createProject, createSequence, login, seedCandidates } from './support.js';

/**
 * The sequence editor's preview keeps up on its own.
 *
 * It used to wait for a click on Preview, refuse to run while there were
 * unsaved edits, render against an arbitrary contact from the CRM rather than
 * someone in the sequence, and never say which mailbox the email was from --
 * so the sign-off, which follows the sending mailbox, was taken on trust.
 *
 * Artifact: test-results/preview-live-artifact/ holds the panel showing an
 * unsaved edit rendered for the enrolled person, with its From line.
 */
const ARTIFACT_DIR = path.join('test-results', 'preview-live-artifact');

test.describe('live sequence preview', () => {
  test.beforeAll(() => fs.mkdirSync(ARTIFACT_DIR, { recursive: true }));

  test('previews the enrolled person on its own, follows unsaved edits, and names the sender', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'preview-live');
    const ids = await seedCandidates(request, org, 'live');
    const project = await createProject(request, org, 'Live Preview Role');
    await addToProject(request, org, project.id, ids);
    const sequence = await createSequence(request, org, project.id, {
      steps: [
        {
          delay_days: 1,
          subject_template: 'Quick note for {{ first_name }}',
          body_template: 'Saved text for {{ first_name }}.\n\nBest,\n{{ sender_first_name }}',
        },
      ],
    });
    // Enrol Grace, not Ada: the CRM sample would list Ada first, so seeing
    // Grace proves the preview starts from the sequence's own people.
    const grace = ids[1];
    const enrolled = await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: [grace] },
    });
    expect(enrolled.ok()).toBeTruthy();

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    // No click: the preview renders for the enrolled person straight away.
    const body = page.getByTestId('preview-body');
    await expect(body).toContainText('Saved text for Grace');
    await expect(page.getByTestId('preview-person')).toHaveValue(grace);
    // It says who the email is from (no mailbox is connected in this stack).
    await expect(page.getByTestId('preview-from')).toContainText('From');
    await expect(page.getByTestId('preview-from')).toContainText('no mailbox connected');

    // Edit without saving: the preview follows a moment after typing stops.
    const editor = page.getByTestId('email-body-editor');
    await editor.click();
    await page.keyboard.press('Control+A');
    await page.keyboard.type('Unsaved line for {{ first_name }}');
    await expect(page.getByTestId('preview-unsaved')).toBeVisible();
    await expect(body).toContainText('Unsaved line for Grace');
    await expect(body).not.toContainText('Saved text');

    await page.getByTestId('preview-person').screenshot({ path: path.join(ARTIFACT_DIR, 'person-picker.png') });
    await page.locator('.card', { has: page.getByTestId('preview-body') }).screenshot({
      path: path.join(ARTIFACT_DIR, 'preview-panel.png'),
    });

    // Previewing is not saving.
    const saved = await (
      await request.get(`/v1/sequences/${sequence.id}`, { headers: org.headers })
    ).json();
    expect(saved.steps[0].body_template).toContain('Saved text for {{ first_name }}');

    // Switching person re-renders too, still without a click.
    await page.getByTestId('preview-person').selectOption(ids[0]);
    await expect(body).toContainText('Unsaved line for Ada');
  });
});
