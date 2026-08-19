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

test.describe('enrolment send mode', () => {
  test('a scheduled first step shows the choice, and right away really sends', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'sendmode');
    const ids = await seedCandidates(request, org, 'sendmode');
    const project = await createProject(request, org, 'SendMode Role');
    await addToProject(request, org, project.id, ids);
    // First step pinned to a time that has certainly passed today or is far
    // out — either way, default enrolment would NOT send immediately. The
    // follow-up step reproduces the reported bug: after a send-now, the row
    // used to show only the follow-up's schedule ("tomorrow 9:00"), reading
    // as if the first email never went.
    const sequence = await createSequence(request, org, project.id, {
      steps: [
        {
          delay_days: 0,
          send_at_time: '09:00',
          send_timezone: 'America/Los_Angeles',
          subject_template: 'Hi {{first_name}}',
          body_template: 'Hello {{first_name}}',
        },
        {
          delay_days: 1,
          send_at_time: '09:00',
          send_timezone: 'America/Los_Angeles',
          subject_template: 'Re: Hi {{first_name}}',
          body_template: 'Following up.',
        },
      ],
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);
    await page.getByRole('button', { name: '+ Add people' }).click();

    const picker = page.getByTestId('candidate-picker');
    await expect(picker.getByTestId('send-mode')).toContainText('at the scheduled time');
    await expect(picker.getByTestId('send-mode')).toContainText('right away');

    await picker.getByTestId('picker-row').filter({ hasText: 'Ada sendmode' }).locator('input').check();
    await picker.getByTestId('send-mode').getByText('right away').click();
    await picker.getByRole('button', { name: /^Enrol/ }).click();

    // "Right away" bypasses the 09:00 slot: the send happens inside the
    // enroll request itself, so the notice reports a definite count — never
    // the worker-fallback wording, and never a stuck "Enrolling…" button.
    await expect(page.getByText('1 email sent')).toBeVisible();
    await expect(page.getByRole('button', { name: /Enrolling/ })).toHaveCount(0);

    // The row must say the first email WENT and that the pending schedule
    // belongs to step 2 — the exact ambiguity that read as a lost send.
    const row = page.getByTestId('enrollment-row').first();
    await expect(row).toContainText('Ada sendmode');
    await expect(row).toContainText('✓1 sent');
    await expect(row).toContainText('next: step 2');
  });

  test('the default keeps the scheduled slot', async ({ page, request }) => {
    const org = await createOrg(request, 'sendmode-def');
    const ids = await seedCandidates(request, org, 'sendmode-def');
    const project = await createProject(request, org, 'SendModeDef Role');
    await addToProject(request, org, project.id, ids);
    const sequence = await createSequence(request, org, project.id, {
      steps: [
        {
          delay_days: 0,
          send_at_time: '09:00',
          send_timezone: 'America/New_York',
          subject_template: 'Hi {{first_name}}',
          body_template: 'Hello {{first_name}}',
        },
      ],
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);
    await page.getByRole('button', { name: '+ Add people' }).click();

    const picker = page.getByTestId('candidate-picker');
    await picker.getByTestId('picker-row').filter({ hasText: 'Grace sendmode-def' }).locator('input').check();
    await picker.getByRole('button', { name: /^Enrol/ }).click();

    await expect(page.getByText(/scheduled time/)).toBeVisible();
    // The enrolment row shows a real future timestamp, not an immediate send.
    await expect(page.getByTestId('enrollment-row').first()).toContainText('next:');
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
