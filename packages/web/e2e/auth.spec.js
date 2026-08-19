import { expect, test } from '@playwright/test';

import { PASSWORD, createOrg, login } from './support.js';

test.describe('sign in', () => {
  test('valid credentials reach the app shell', async ({ page, request }) => {
    const org = await createOrg(request, 'auth');
    await login(page, org.email);

    await expect(page.getByRole('navigation')).toBeVisible();
    await expect(
      page.getByRole('navigation').getByRole('link', { name: 'Sequences' }).first(),
    ).toBeVisible();
  });

  test('a wrong password keeps you on the form with an error', async ({ page, request }) => {
    const org = await createOrg(request, 'auth-bad');
    await page.goto('/');
    await page.getByPlaceholder('Email').fill(org.email);
    await page.getByPlaceholder('Password').fill(`${PASSWORD}-wrong`);
    await page.locator('button[type="submit"]').click();

    await expect(page.locator('button[type="submit"]')).toBeVisible();
    await expect(page.getByRole('navigation')).toHaveCount(0);
  });

  test('signing out returns to the login form', async ({ page, request }) => {
    const org = await createOrg(request, 'auth-out');
    await login(page, org.email);
    await page.getByRole('button', { name: 'Log out' }).click();

    await expect(page.locator('button[type="submit"]')).toBeVisible();
  });
});
