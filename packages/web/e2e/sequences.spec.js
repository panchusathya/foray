import { expect, test } from '@playwright/test';

import {
  addToProject,
  createOrg,
  createProject,
  createSequence,
  login,
  seedCandidates,
} from './support.js';

/** Project + candidates + a one-step sequence, all wired together. */
async function scenario(request, tag) {
  const org = await createOrg(request, tag);
  const ids = await seedCandidates(request, org, tag);
  const project = await createProject(request, org, `${tag} Role`);
  await addToProject(request, org, project.id, ids);
  const sequence = await createSequence(request, org, project.id);
  return { org, ids, project, sequence };
}

test.describe('enrolment lives in the sequence editor', () => {
  test('the picker offers the project pipeline and enrols the chosen people', async ({
    page,
    request,
  }) => {
    const { org, sequence } = await scenario(request, 'enrol');
    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    await expect(page.getByText('Nobody enrolled yet')).toBeVisible();
    await page.getByRole('button', { name: '+ Add people' }).click();

    const picker = page.getByTestId('candidate-picker');
    await expect(picker.getByTestId('picker-row').filter({ hasText: 'Ada enrol' })).toBeVisible();

    await picker.getByTestId('picker-row').filter({ hasText: 'Ada enrol' }).locator('input').check();
    await picker.getByRole('button', { name: /^Enrol/ }).click();

    // Enrolled people show by name, not by candidate id.
    const rows = page.getByTestId('enrollment-row');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText('Ada enrol');
    await expect(page.getByText('Nobody enrolled yet')).toHaveCount(0);
  });

  test('someone already enrolled is not offered again', async ({ page, request }) => {
    const { org, ids, sequence } = await scenario(request, 'noredupe');
    await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: [ids[0]] },
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);
    await page.getByRole('button', { name: '+ Add people' }).click();

    const picker = page.getByTestId('candidate-picker');
    await expect(picker.getByTestId('picker-row').filter({ hasText: 'Ada noredupe' })).toHaveCount(0);
    await expect(
      picker.getByTestId('picker-row').filter({ hasText: 'Grace noredupe' }),
    ).toBeVisible();
  });

  test('re-enrolling the same person through the API stays a single enrolment', async ({
    page,
    request,
  }) => {
    const { org, ids, sequence } = await scenario(request, 'idem');
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const response = await request.post(`/v1/sequences/${sequence.id}/enroll`, {
        headers: org.headers,
        data: { candidate_ids: [ids[0]] },
      });
      expect(response.ok()).toBeTruthy();
    }

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);
    const rows = page.getByTestId('enrollment-row');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText('Ada idem');
  });

  test('a candidate with no email is flagged before you enrol them', async ({ page, request }) => {
    const { org, sequence } = await scenario(request, 'noemail');
    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);
    await page.getByRole('button', { name: '+ Add people' }).click();

    const picker = page.getByTestId('candidate-picker');
    const row = picker.getByTestId('picker-row').filter({ hasText: 'Noemail noemail' });
    await expect(row.getByText('no email')).toBeVisible();

    await row.locator('input').check();
    await expect(picker.getByText(/have no email yet/)).toBeVisible();
  });

  test('a paused sequence says nothing will send and offers Activate', async ({ page, request }) => {
    const { org, ids, sequence } = await scenario(request, 'paused');
    await request.patch(`/v1/sequences/${sequence.id}/status?value=paused`, {
      headers: org.headers,
    });
    await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: [ids[0]] },
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    const banner = page.getByTestId('paused-banner');
    await expect(banner).toContainText(/nothing sends while the sequence is paused/i);
    await expect(banner.getByRole('button', { name: 'Activate' })).toBeVisible();
  });

  test('editing steps is blocked while enrolments are mid-flight', async ({ page, request }) => {
    // Steps index into live enrolments via current_step, so the backend 409s
    // and the editor must offer the pause-and-save escape hatch.
    const { org, ids, sequence } = await scenario(request, 'midflight');
    await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: [ids[0]] },
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    await page.getByPlaceholder('Subject').fill('Edited subject line');
    await page.getByRole('button', { name: 'Save' }).click();

    await expect(page.getByText(/Pause & save|pause the sequence/i).first()).toBeVisible();
  });

  test('pausing keeps unsaved edits instead of discarding them', async ({ page, request }) => {
    // Pausing is what the 409 above tells you to do, so a reload that resets
    // the form would throw away the very edit the user was trying to save.
    const { org, ids, sequence } = await scenario(request, 'keepedits');
    await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: [ids[0]] },
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    await page.getByPlaceholder('Subject').fill('Survives the pause');
    await page.getByRole('button', { name: 'Pause' }).click();

    await expect(page.getByPlaceholder('Subject')).toHaveValue('Survives the pause');
  });
});

test.describe('step scheduling', () => {
  // A step with a send time does NOT send on enrolment, so the preview must
  // not claim it does — this caught the editor contradicting the sequencer.
  test('a first step with a send time shows the time, not "on enrollment"', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'sched');
    const project = await createProject(request, org, 'Sched Role');
    const sequence = await createSequence(request, org, project.id, {
      steps: [
        {
          delay_days: 0,
          send_at_time: '09:00',
          send_timezone: 'America/New_York',
          subject_template: 'Morning note',
          body_template: 'Hi {{ first_name }}',
        },
      ],
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    const stepCard = page.locator('.step-item').first();
    await expect(stepCard).toContainText('09:00');
    await expect(stepCard).not.toContainText('on enrollment');
  });
});
