import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

/**
 * Editing a person's addresses, and deleting a person.
 *
 * Both existed only as API endpoints until a provider returned fabricated data:
 * ContactOut answered HTTP 200 with a sample profile once its quota ran out, and
 * 30 records ended up carrying email1@example.com / email2@gmail.com with no way
 * to correct them from the UI.
 *
 * The load-bearing case is the ordering one. emails[0] is
 * Candidate.primary_email and is what send_step sends to, so "Make primary" has
 * a real consequence and is asserted through the API rather than by reading the
 * DOM back.
 */
test.describe('contact email editing', () => {
  async function person(request, tag, emails) {
    const org = await createOrg(request, tag);
    const response = await request.post('/v1/candidates', {
      headers: org.headers,
      data: { full_name: 'Yash Gupta', emails, source: 'foray' },
    });
    if (!response.ok()) {
      throw new Error(`create failed (${response.status()}): ${await response.text()}`);
    }
    return { org, id: (await response.json()).id };
  }

  async function addresses(request, org, id) {
    const response = await request.get(`/v1/candidates/${id}`, { headers: org.headers });
    return (await response.json()).emails;
  }

  test('a fabricated address can be removed', async ({ page, request }) => {
    const { org, id } = await person(request, 'mail-rm', [
      'email2@gmail.com',
      'yash@stripe.com',
    ]);

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);

    const editor = page.getByTestId('email-editor');
    await expect(editor).toContainText('email2@gmail.com');
    // Flagged, because it is the placeholder ContactOut hands back and it is
    // syntactically perfect -- nothing else would mark it out.
    await expect(editor).toContainText('looks fake');

    // Removing the primary of several warns that the next one is promoted.
    page.once('dialog', (dialog) => dialog.accept());
    await editor.getByRole('button', { name: 'Remove email2@gmail.com' }).click();

    await expect(editor).not.toContainText('email2@gmail.com');
    expect(await addresses(request, org, id)).toEqual(['yash@stripe.com']);
  });

  test('make primary reorders the list, which changes the send address', async ({
    page,
    request,
  }) => {
    const { org, id } = await person(request, 'mail-primary', [
      'yash@bigcorp.com',
      'yash.gupta@gmail.com',
    ]);

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);

    const editor = page.getByTestId('email-editor');
    await editor.getByRole('button', { name: 'Make primary' }).click();

    // Asserted through the API: the DOM order is a rendering of this, and this
    // is what the sequencer reads.
    await expect
      .poll(() => addresses(request, org, id))
      .toEqual(['yash.gupta@gmail.com', 'yash@bigcorp.com']);
  });

  test('an address can be added, and nonsense is refused', async ({ page, request }) => {
    const { org, id } = await person(request, 'mail-add', []);

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);

    const editor = page.getByTestId('email-editor');
    await expect(editor).toContainText('cannot be enrolled in a sequence');

    await editor.locator('input').fill('not-an-address');
    await editor.getByRole('button', { name: 'Add' }).click();
    await expect(editor).toContainText('is not an email address');
    expect(await addresses(request, org, id)).toEqual([]);

    await editor.locator('input').fill('yash.gupta@gmail.com');
    await editor.getByRole('button', { name: 'Add' }).click();
    await expect.poll(() => addresses(request, org, id)).toEqual(['yash.gupta@gmail.com']);
  });

  test('a contact can be deleted, and the dialog says what goes with them', async ({
    page,
    request,
  }) => {
    const { org, id } = await person(request, 'mail-del', ['yash@stripe.com']);

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);

    let message = '';
    page.once('dialog', (dialog) => {
      message = dialog.message();
      dialog.accept();
    });
    await page.getByTestId('delete-candidate').click();

    await page.waitForURL('**/contacts');
    expect(message).toContain('Yash Gupta');
    expect(message).toContain('no undo');
    // Points at the cheaper fix, since a bad address is the usual reason
    // somebody reaches for delete.
    expect(message).toContain('Email addresses');

    const gone = await request.get(`/v1/candidates/${id}`, { headers: org.headers });
    expect(gone.status()).toBe(404);
  });

  test('cancelling the delete dialog leaves the person alone', async ({ page, request }) => {
    const { org, id } = await person(request, 'mail-keep', ['yash@stripe.com']);

    await login(page, org.email);
    await page.goto(`/contacts/${id}`);

    page.once('dialog', (dialog) => dialog.dismiss());
    await page.getByTestId('delete-candidate').click();

    const still = await request.get(`/v1/candidates/${id}`, { headers: org.headers });
    expect(still.status()).toBe(200);
  });
});
