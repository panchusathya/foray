import { expect, test } from '@playwright/test';

import { createOrg, login, seedCandidates } from './support.js';

/**
 * The person record: notes, inline editing, and the status control.
 *
 * The load-bearing case is the last one. `do_not_contact` is only meaningful
 * if the sequencer actually honours it, so the test asserts the suppression
 * exists rather than that the dropdown changed.
 */
test.describe('contact record', () => {
  async function person(request, tag) {
    const org = await createOrg(request, tag);
    const ids = await seedCandidates(request, org, tag);
    return { org, id: ids[0] };
  }

  test('emails is the default tab and is there on first paint', async ({ page, request }) => {
    // Lazy-mounting the tabs is the obvious optimisation and would break the
    // reason people open this page.
    const { org, id } = await person(request, 'rec-default');

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);

    await expect(page.getByTestId('contact-history')).toBeVisible();
    await expect(page.getByTestId('candidate-profile')).toBeVisible();
    await expect(page.getByTestId('record-tabs')).toBeVisible();
  });

  test('notes can be written, edited and deleted', async ({ page, request }) => {
    const { org, id } = await person(request, 'rec-notes');

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);
    await page.getByTestId('tab-notes').click();

    await page.getByTestId('note-composer').fill('Spoke at the meetup.');
    await page.getByRole('button', { name: 'Add note' }).click();

    const note = page.getByTestId('note').first();
    await expect(note).toBeVisible();
    await expect(note).toContainText('Spoke at the meetup.');
    // The byline is snapshotted, so it survives the author being removed.
    await expect(note.locator('.byline')).toContainText('today');

    await page.getByTestId('note-edit').click();
    await note.locator('textarea').fill('Spoke at the meetup — wants staff scope.');
    await page.getByRole('button', { name: 'Save' }).click();
    await expect(note).toContainText('wants staff scope');
    await expect(note.locator('.byline')).toContainText('edited');

    page.once('dialog', (dialog) => dialog.accept());
    await page.getByTestId('note-delete').click();
    await expect(page.getByTestId('note')).toHaveCount(0);
  });

  test('a note survives a reload', async ({ page, request }) => {
    // Catches an optimistic-only bug where nothing was actually POSTed.
    const { org, id } = await person(request, 'rec-persist');

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);
    await page.getByTestId('tab-notes').click();
    await page.getByTestId('note-composer').fill('Durable.');
    await page.getByRole('button', { name: 'Add note' }).click();
    await expect(page.getByTestId('note')).toHaveCount(1);

    await page.reload();
    await page.getByTestId('tab-notes').click();
    await expect(page.getByTestId('note')).toContainText('Durable.');
  });

  test('switching tabs leaves the email history intact', async ({ page, request }) => {
    const { org, id } = await person(request, 'rec-tabs');

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);
    await page.getByTestId('tab-notes').click();
    await expect(page.getByTestId('notes-panel')).toBeVisible();

    await page.getByTestId('tab-emails').click();
    await expect(page.getByTestId('contact-history')).toBeVisible();
  });

  test('a field can be edited in place and it sticks', async ({ page, request }) => {
    const { org, id } = await person(request, 'rec-inline');

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);

    await page.getByTestId('field-current_title').click();
    await page.getByTestId('field-current_title').fill('Head of Platform');
    await page.getByTestId('field-current_title').press('Enter');

    await expect(page.getByTestId('field-current_title')).toContainText('Head of Platform');
    await page.reload();
    await expect(page.getByTestId('field-current_title')).toContainText('Head of Platform');
  });

  test('do-not-contact writes a suppression the sequencer will honour', async ({
    page,
    request,
  }) => {
    const { org, id } = await person(request, 'rec-dnc');

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);
    await page.getByTestId('crm-status').selectOption('do_not_contact');
    await expect(page.getByTestId('dnc-badge')).toBeVisible();

    // The status is only real if it reaches the list the sender checks.
    const suppressed = await request.get('/v1/suppressions', { headers: org.headers });
    expect(await suppressed.json()).not.toHaveLength(0);

    await page.getByTestId('crm-status').selectOption('active');
    await expect(page.getByTestId('dnc-badge')).toHaveCount(0);

    const lifted = await request.get('/v1/suppressions', { headers: org.headers });
    expect(await lifted.json()).toHaveLength(0);
  });
});
