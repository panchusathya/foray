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

  test('a stop made from the outbox can be undone on the row itself', async ({
    page,
    request,
  }) => {
    const { org, ids, sequence } = await scenario(request, 'undo');
    // A day's delay keeps the enrolment scheduled rather than due, so the
    // worker cannot send it out from under the test.
    await request.put(`/v1/sequences/${sequence.id}/steps`, {
      headers: org.headers,
      data: [
        {
          delay_days: 1,
          subject_template: 'Quick note for {{ first_name }}',
          body_template: 'Hi {{ first_name }}.',
        },
      ],
    });
    const enrolled = await (
      await request.post(`/v1/sequences/${sequence.id}/enroll`, {
        headers: org.headers,
        data: { candidate_ids: [ids[0], ids[1]] },
      })
    ).json();
    const stopped = enrolled.enrollments.find((row) => row.candidate_id === ids[0]);
    await request.post('/v1/outreach/in-flight/cancel', {
      headers: org.headers,
      data: { enrollment_ids: [stopped.id], action: 'stop' },
    });

    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    // The stopped row says who stopped it and offers the way back; the other
    // row, still active, offers nothing of the kind.
    const rows = page.getByTestId('enrollment-row');
    const undoable = rows.filter({ hasText: 'Ada undo' });
    await expect(undoable).toContainText('cancelled by you');
    await expect(undoable.getByTestId('enrollment-reenrol')).toBeVisible();
    await expect(rows.filter({ hasText: 'Grace undo' }).getByTestId('enrollment-reenrol')).toHaveCount(0);

    await undoable.getByTestId('enrollment-reenrol').click();
    const panel = page.getByTestId('reenrol-panel');
    await expect(panel).toContainText('Re-enrol Ada undo');
    await panel.getByTestId('reenrol-submit').click();

    await expect(page.getByText(/Re-enrolled 1/)).toBeVisible();
    await expect(undoable).toContainText('active');
    await expect(undoable).not.toContainText('cancelled by you');
    await expect(page.getByTestId('reenrol-panel')).toHaveCount(0);
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
    // Assert the consequence, not just the count. This used to say they "wait
    // until one is found", which was false -- send_step stops such an enrollment
    // permanently and nothing re-arms it -- so the warning being accurate is the
    // thing worth guarding.
    await expect(picker.getByText(/1 of 1 have no email/)).toBeVisible();
    await expect(picker.getByText(/permanently/)).toBeVisible();
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

test.describe('sequence email rich text', () => {
  test('bold and link round-trip in the body editor', async ({ page, request }) => {
    const { org, sequence } = await scenario(request, 'rte');
    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    const editor = page.getByTestId('email-body-editor');
    await expect(editor).toBeVisible();
    await editor.click();
    await page.keyboard.press('Control+A');
    await page.keyboard.press('Backspace');
    await page.keyboard.type('Hello world');
    await page.keyboard.press('Control+A');
    await page.getByRole('button', { name: 'Bold' }).click();
    await expect(editor.locator('strong')).toContainText('Hello world');

    page.once('dialog', (dialog) => dialog.accept('https://jobs.example.com/role'));
    await page.getByRole('button', { name: 'Link' }).click();
    await expect(editor.locator('a[href="https://jobs.example.com/role"]')).toContainText(
      'Hello world',
    );

    await page.getByRole('button', { name: 'Save' }).click();
    await expect(page.getByRole('button', { name: 'Saved' })).toBeVisible();
    await page.reload();
    // TipTap wraps the link around the bold mark (`<a><strong>`), not the reverse.
    const reloaded = page.getByTestId('email-body-editor');
    await expect(reloaded.locator('strong')).toContainText('Hello world');
    await expect(reloaded.locator('a[href="https://jobs.example.com/role"]')).toContainText(
      'Hello world',
    );
  });
});

test.describe('a new sequence lands in the project you are in', () => {
  /** Two projects, with the only existing sequence living in the first. */
  async function twoProjects(request, tag) {
    const org = await createOrg(request, tag);
    // Created first, so it is *older*: /v1/projects is newest-first, which is
    // what made the source project the head of the org-wide clone list.
    const source = await createProject(request, org, `${tag} Baseten`);
    const target = await createProject(request, org, `${tag} Startups`);
    const sequence = await createSequence(request, org, source.id, {
      name: `${tag} founding ML`,
    });
    return { org, source, target, sequence };
  }

  test('cloning another project’s sequence puts the copy in this project', async ({
    page,
    request,
  }) => {
    // The reported bug: the clone dialog is fed the workspace-wide sequence
    // list, the copy used to inherit the source's project, and the Sequences
    // page filters by project -- so the copy landed under Baseten while the
    // switcher said Startups, and was nowhere to be seen.
    const { org, source, target, sequence } = await twoProjects(request, 'clone');
    await login(page, org.email);
    await page.goto('/sequences');

    await page.locator('#project-switcher').selectOption({ label: `${target.name}` });
    await expect(page.getByText('No sequences for this project yet')).toBeVisible();

    await page.getByRole('button', { name: 'New sequence' }).click();
    await page.getByRole('button', { name: /Clone an existing sequence/ }).click();

    // The source list is workspace-wide, so it says which project each one is in.
    await expect(page.getByTestId('clone-source')).toContainText(source.name);
    // And the copy's destination defaults to the project in the switcher.
    await expect(page.getByTestId('sequence-project')).toHaveValue(target.id);

    await page.getByRole('button', { name: 'Clone' }).click();

    await expect(page).toHaveURL(/\/sequences\/seq/);
    await expect(page.getByTestId('sequence-project-picker')).toHaveValue(target.id);

    // And it is on the page it was created from, not the source's.
    await page.goto('/sequences');
    await expect(page.locator('#project-switcher')).toHaveValue(target.id);
    await expect(page.getByTestId('sequence-row')).toHaveCount(1);
    await expect(page.getByTestId('sequence-row')).toContainText('(copy)');

    await page.locator('#project-switcher').selectOption({ label: `${source.name}` });
    const sourceRows = page.getByTestId('sequence-row');
    await expect(sourceRows).toHaveCount(1);
    await expect(sourceRows).toContainText(sequence.name);
    await expect(sourceRows).not.toContainText('(copy)');
  });

  test('a stale ?project= does not outrank the switcher', async ({ page, request }) => {
    // The Jobs pitch button lands here as /sequences?new=1&project=<id>. Left
    // in the URL, that param won every later open of the modal, so the picker
    // could name one project while the list under it showed another.
    const { org, source, target } = await twoProjects(request, 'stale');
    await login(page, org.email);
    await page.goto(`/sequences?new=1&project=${source.id}`);

    // Seeded from the URL on the first open.
    await page.getByRole('button', { name: '+ Start from scratch' }).click();
    await expect(page.getByTestId('sequence-project')).toHaveValue(source.id);
    await page.getByRole('button', { name: 'Close' }).click();

    // ...and then consumed, so it cannot come back.
    await expect(page).toHaveURL(/\/sequences$/);

    await page.locator('#project-switcher').selectOption({ label: `${target.name}` });
    await page.getByRole('button', { name: 'New sequence' }).click();
    await page.getByRole('button', { name: '+ Start from scratch' }).click();
    await expect(page.getByTestId('sequence-project')).toHaveValue(target.id);
  });

  test('a sequence in the wrong project can be moved from the editor', async ({
    page,
    request,
  }) => {
    // How the already-misfiled one gets fixed. PATCH /v1/sequences/{id} always
    // took project_id; nothing in the UI was calling it.
    const { org, source, target, sequence } = await twoProjects(request, 'move');
    await login(page, org.email);
    await page.goto(`/sequences/${sequence.id}`);

    await expect(page.getByTestId('sequence-project-picker')).toHaveValue(source.id);
    await page.getByTestId('sequence-project-picker').selectOption(target.id);
    await page.getByRole('button', { name: 'Save' }).click();
    await expect(page.getByRole('button', { name: 'Saved' })).toBeVisible();

    await page.goto('/sequences');
    await page.locator('#project-switcher').selectOption({ label: `${target.name}` });
    await expect(page.getByTestId('sequence-row')).toContainText(sequence.name);
    await page.locator('#project-switcher').selectOption({ label: `${source.name}` });
    await expect(page.getByText('No sequences for this project yet')).toBeVisible();
  });

  test('a sequence in no project is admitted to, not hidden', async ({ page, request }) => {
    // It is filtered out of every project, so without this it reads as lost.
    const org = await createOrg(request, 'orphan');
    const project = await createProject(request, org, 'Orphan Role');
    await createSequence(request, org, null, { name: 'Unfiled outreach' });
    await login(page, org.email);
    await page.goto('/sequences');
    await expect(page.locator('#project-switcher')).toHaveValue(project.id);

    const panel = page.getByTestId('orphan-sequences');
    await expect(panel).toContainText('1 sequence in no project');
    await expect(page.getByTestId('sequence-row')).toHaveCount(0);

    await page.getByTestId('orphan-toggle').click();
    await expect(panel.getByTestId('sequence-row')).toContainText('Unfiled outreach');
  });
});
