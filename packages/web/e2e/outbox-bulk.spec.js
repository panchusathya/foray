import fs from 'node:fs';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { addToProject, createOrg, createProject, createSequence, login } from './support.js';

/**
 * Acting on a whole selection, not one page of it, and splitting a sequence's
 * people by how far they have got.
 *
 * 105 people is one more page than the Outbox shows, which is the case that
 * used to fail: the header checkbox took 100, turning the page wiped them, and
 * the bulk bar had scrolled out of sight by the time a row near the bottom was
 * ticked. Rows here are *scheduled* (a day's delay), so no worker is needed
 * and nothing can send out from under the assertions.
 *
 * Artifact: test-results/outbox-bulk-artifact/ holds the Outbox with every
 * matching row selected and the bar pinned mid-scroll, and the sequence's
 * enrolment list split by step.
 */
const ARTIFACT_DIR = path.join('test-results', 'outbox-bulk-artifact');
const PEOPLE = 105;
const SKIPPED = 5;

async function seedMany(request, org, tag, count) {
  const candidates = Array.from({ length: count }, (_, i) => ({
    source: 'e2e',
    source_id: `${tag}-${i}`,
    full_name: `Person${String(i).padStart(3, '0')} ${tag}`,
    emails: [`p${i}-${tag}@e2e.example.com`],
  }));
  const response = await request.post('/v1/candidates/bulk', {
    headers: org.headers,
    data: { candidates },
  });
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  return body.ids;
}

test.describe('outbox bulk selection and step progress', () => {
  test.beforeAll(() => fs.mkdirSync(ARTIFACT_DIR, { recursive: true }));

  test('select-all covers every page, survives paging, and a bulk stop takes the lot', async ({
    page,
    request,
  }) => {
    test.setTimeout(90_000);
    const org = await createOrg(request, 'outbox-bulk');
    const ids = await seedMany(request, org, 'bulk', PEOPLE);
    expect(ids).toHaveLength(PEOPLE);
    const project = await createProject(request, org, 'Bulk Role');
    await addToProject(request, org, project.id, ids);
    const step = (subject) => ({
      delay_days: 1,
      subject_template: subject,
      body_template: 'Hi {{ first_name }}, about {{ role_title | the role }}.',
    });
    const sequence = await createSequence(request, org, project.id, {
      name: 'Bulk outreach',
      steps: [step('First note'), step('Following up')],
    });
    const enrolled = await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: ids },
    });
    expect(enrolled.ok()).toBeTruthy();

    // Skip step 1 for five people so the line holds two steps.
    const first = await (
      await request.get('/v1/outreach/in-flight?limit=500', { headers: org.headers })
    ).json();
    expect(first.total).toBe(PEOPLE);
    const skipIds = first.rows.slice(0, SKIPPED).map((row) => row.enrollment_id);
    const skipped = await request.post('/v1/outreach/in-flight/cancel', {
      headers: org.headers,
      data: { enrollment_ids: skipIds, action: 'skip' },
    });
    expect((await skipped.json()).skipped).toBe(SKIPPED);

    await login(page, org.email);
    await page.goto('/outbox');
    const grid = page.getByTestId('outbox-grid');
    await expect(grid.locator('tbody tr')).toHaveCount(100);

    // The step picker names each step with its size.
    const stepPicker = page.getByTestId('outbox-step');
    await expect(stepPicker.locator('option')).toHaveText([
      'All steps',
      `Step 1 (first email) · ${PEOPLE - SKIPPED}`,
      `Step 2 · ${SKIPPED}`,
    ]);

    // The header box takes the page; the bar then offers the rest.
    await page.getByTestId('outbox-select-page').check();
    await expect(page.getByTestId('outbox-selected')).toHaveText('100 selected');
    await page.getByTestId('outbox-select-all').click();
    await expect(page.getByTestId('outbox-selected')).toHaveText(`${PEOPLE} selected`);
    await expect(page.getByTestId('outbox-actions')).toContainText(
      `All ${PEOPLE} matching, across every page`,
    );

    // Turning the page keeps the selection.
    await page.getByTestId('outbox-pager').getByText('Next →').click();
    await expect(grid.locator('tbody tr')).toHaveCount(PEOPLE - 100);
    await expect(page.getByTestId('outbox-selected')).toHaveText(`${PEOPLE} selected`);
    await page.getByTestId('outbox-pager').getByText('← Previous').click();

    // Scrolled to the bottom of the list, the bar is still on screen.
    await grid.locator('tbody tr').last().scrollIntoViewIfNeeded();
    const bar = page.getByTestId('outbox-actions');
    await expect(bar).toBeInViewport();
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'outbox-all-selected.png') });

    // A step filter is a new set of rows: the selection goes.
    await stepPicker.selectOption(String(1));
    await expect(grid.locator('tbody tr')).toHaveCount(SKIPPED);
    await expect(page.getByTestId('outbox-actions')).toHaveCount(0);

    // Select every step-1 row (two pages' worth) and stop them in one go.
    await stepPicker.selectOption(String(0));
    await expect(grid.locator('tbody tr')).toHaveCount(PEOPLE - SKIPPED);
    await page.getByTestId('outbox-select-page').check();
    await expect(page.getByTestId('outbox-select-all')).toHaveCount(0); // 100 is all of them
    page.once('dialog', (dialog) => dialog.accept());
    await page.getByTestId('outbox-bulk-stop').click();
    await expect(page.getByTestId('outbox-result')).toContainText(`${PEOPLE - SKIPPED} stopped`);

    const after = await (
      await request.get(`/v1/sequences/${sequence.id}/enrollments`, { headers: org.headers })
    ).json();
    expect(after.filter((e) => e.status === 'stopped')).toHaveLength(PEOPLE - SKIPPED);
    expect(after.filter((e) => e.status === 'active')).toHaveLength(SKIPPED);

    // The sequence's list splits the same people by step.
    await page.goto(`/sequences/${sequence.id}`);
    const progress = page.getByTestId('enrollment-progress');
    await expect(progress.getByTestId('enrollment-progress-chip')).toHaveText([
      `Not sent yet ${PEOPLE - SKIPPED}`,
      `Step 1 sent ${SKIPPED}`,
      'Step 2 sent 0',
    ]);
    await progress.getByText('Step 1 sent').click();
    await expect(page.getByTestId('enrollment-row')).toHaveCount(SKIPPED);

    // Progress narrows inside a status: "stopped" leaves only not-sent-yet.
    await progress.getByText('Step 1 sent').click(); // clear it
    await page.locator('.enrol-chip', { hasText: /^stopped/i }).click();
    await expect(progress.getByTestId('enrollment-progress-chip')).toHaveText([
      `Not sent yet ${PEOPLE - SKIPPED}`,
      'Step 1 sent 0',
      'Step 2 sent 0',
    ]);
    await page.locator('.enrol-chip', { hasText: /^stopped/i }).click();
    await page.getByTestId('enrollment-list').screenshot({
      path: path.join(ARTIFACT_DIR, 'sequence-progress.png'),
    });
  });
});
