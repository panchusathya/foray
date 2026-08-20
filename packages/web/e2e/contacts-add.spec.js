import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

/**
 * Adding a contact by hand.
 *
 * The duplicate case is the one worth guarding: hitting it usually means the
 * person is already in Contacts, so the modal has to offer a way through
 * rather than an error. That only works if the API's 409 carries the existing
 * record and the client reads it off `.detail` — ApiError renders a dict
 * detail as "Request failed (409)", so the message alone is useless.
 */
test.describe('add contact', () => {
  test('creates a person and lands on their record', async ({ page, request }) => {
    const org = await createOrg(request, 'add-basic');

    await login(page, org.email);
    await page.goto('/contacts');
    await page.getByTestId('add-contact').click();

    await expect(page.getByTestId('add-contact-modal')).toBeVisible();
    // A nameless contact is not a contact.
    await expect(page.getByTestId('add-save')).toBeDisabled();

    await page.getByTestId('add-name').fill('Nadia Okoro');
    await page.getByTestId('add-email').fill('nadia@example.com');
    await page.getByTestId('add-title').fill('Infra Lead');
    await page.getByTestId('add-save').click();

    await expect(page).toHaveURL(/\/contacts\/cand_/);
    await expect(page.getByRole('heading', { name: 'Nadia Okoro' })).toBeVisible();
  });

  test('an optional first note is saved with them', async ({ page, request }) => {
    const org = await createOrg(request, 'add-note');

    await login(page, org.email);
    await page.goto('/contacts');
    await page.getByTestId('add-contact').click();
    await page.getByTestId('add-name').fill('Nadia Okoro');
    await page.getByTestId('add-note').fill('Referred by Quinn.');
    await page.getByTestId('add-save').click();

    await expect(page).toHaveURL(/\/contacts\/cand_/);
    await page.getByTestId('tab-notes').click();
    await expect(page.getByTestId('note')).toContainText('Referred by Quinn.');
  });

  test('a duplicate address offers the existing record', async ({ page, request }) => {
    const org = await createOrg(request, 'add-dupe');

    await login(page, org.email);
    await page.goto('/contacts');

    await page.getByTestId('add-contact').click();
    await page.getByTestId('add-name').fill('Nadia Okoro');
    await page.getByTestId('add-email').fill('nadia@example.com');
    await page.getByTestId('add-save').click();
    await expect(page).toHaveURL(/\/contacts\/cand_/);

    await page.goto('/contacts');
    await page.getByTestId('add-contact').click();
    await page.getByTestId('add-name').fill('N. Okoro');
    await page.getByTestId('add-email').fill('nadia@example.com');
    await page.getByTestId('add-save').click();

    const duplicate = page.getByTestId('add-duplicate');
    await expect(duplicate).toContainText('Nadia Okoro');
    await expect(duplicate).toContainText('nadia@example.com');

    await duplicate.getByRole('link').click();
    await expect(page.getByRole('heading', { name: 'Nadia Okoro' })).toBeVisible();
  });

  test('save-and-add-another keeps the modal open', async ({ page, request }) => {
    const org = await createOrg(request, 'add-another');

    await login(page, org.email);
    await page.goto('/contacts');
    await page.getByTestId('add-contact').click();
    await page.getByTestId('add-name').fill('First Person');
    await page.getByTestId('add-save-another').click();

    await expect(page.getByTestId('add-another-note')).toContainText('First Person');
    await expect(page.getByTestId('add-contact-modal')).toBeVisible();
    await expect(page.getByTestId('add-name')).toHaveValue('');

    await page.getByTestId('add-name').fill('Second Person');
    await page.getByTestId('add-save').click();
    await expect(page).toHaveURL(/\/contacts\/cand_/);
  });

  test('two people with the same name and no email stay two people', async ({
    page,
    request,
  }) => {
    // normalize_candidate derives source_id from the name when there is no
    // email or LinkedIn, so without an explicit id the second would overwrite
    // the first.
    const org = await createOrg(request, 'add-samename');

    await login(page, org.email);
    for (const _ of [0, 1]) {
      await page.goto('/contacts');
      await page.getByTestId('add-contact').click();
      await page.getByTestId('add-name').fill('John Smith');
      await page.getByTestId('add-save').click();
      await expect(page).toHaveURL(/\/contacts\/cand_/);
    }

    await page.goto('/contacts?view=crm&q=John Smith');
    await expect(page.getByTestId('contact-row')).toHaveCount(2);
  });
});
