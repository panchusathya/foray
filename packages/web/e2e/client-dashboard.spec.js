import fs from 'node:fs';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { PASSWORD, createOrg, login } from './support.js';

/**
 * Client dashboards, end to end: we share people with a client, the client
 * logs in with a username, moves someone and leaves a note, and we see both.
 *
 * Artifact: test-results/client-dashboard-artifact/ holds a screenshot of
 * each side and summary.json with what was asserted, regenerated every run.
 */

const ARTIFACT_DIR = path.join('test-results', 'client-dashboard-artifact');

test.describe('client dashboard', () => {
  test('a client moves a candidate and leaves a note; we see both', async ({
    page,
    browser,
    request,
  }) => {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    const summary = {};
    const org = await createOrg(request, 'clientdash');
    const tag = Date.now().toString(36);

    // A contact we already hold, reachable by LinkedIn URL, with details the
    // client must never see.
    const seeded = await request.post('/v1/candidates/bulk', {
      headers: org.headers,
      data: {
        candidates: [
          {
            source: 'e2e',
            source_id: `${tag}-known`,
            full_name: `Known ${tag}`,
            emails: [`known-${tag}@e2e.example.com`],
            linkedin_url: `https://www.linkedin.com/in/known-${tag}`,
            current_title: 'Staff Engineer',
            current_company: 'Initech',
          },
        ],
      },
    });
    expect(seeded.ok()).toBeTruthy();

    const account = await (
      await request.post('/v1/clients', { headers: org.headers, data: { name: `Flowgen ${tag}` } })
    ).json();
    const username = `fg-${tag}`;
    const loginRes = await request.post(`/v1/clients/${account.id}/logins`, {
      headers: org.headers,
      data: { username, password: PASSWORD },
    });
    expect(loginRes.ok()).toBeTruthy();

    // --- recruiter shares a pasted spreadsheet --------------------------------
    await login(page, org.email);
    await page.goto(`/clients/${account.id}`);
    await page.getByRole('button', { name: 'Share candidates' }).click();
    await page
      .getByTestId('share-input')
      .fill(
        [
          'Name\tLinkedIn\tStatus',
          `Known ${tag}\tlinkedin.com/in/known-${tag}/\t1st round`,
          `Rejected Person\thttps://www.linkedin.com/in/rej-${tag}\tDeclined`,
          'Waiting Person',
        ].join('\n'),
      );
    // The header row is recognised and dropped, and statuses are read.
    await expect(page.getByTestId('share-preview')).toContainText('3 to share · 2 with a status');
    await page.getByRole('button', { name: 'Share', exact: true }).click();
    await expect(page.getByTestId('share-result')).toContainText('3 added');
    await expect(page.getByTestId('share-result')).toContainText('1 matched to existing contacts');

    // Re-pasting the same person with a new status updates, not duplicates.
    await page
      .getByTestId('share-input')
      .fill(`https://www.linkedin.com/in/known-${tag}, second round`);
    await page.getByRole('button', { name: 'Share', exact: true }).click();
    await expect(page.getByTestId('share-result')).toContainText('0 added · 1 updated');
    await expect(page.getByTestId('cp-row')).toHaveCount(3);
    await expect(page.getByTestId('cp-pile-rejected')).toContainText('1');
    summary.recruiter_share = 'header dropped, 3 added, 1 matched a contact, re-paste updated';

    // --- the client's side ------------------------------------------------------
    const clientContext = await browser.newContext();
    const clientPage = await clientContext.newPage();
    await clientPage.goto('/');
    await clientPage.getByPlaceholder('Email or username').fill(username);
    await clientPage.getByPlaceholder('Password').fill(PASSWORD);
    await clientPage.locator('button[type="submit"]').click();
    await expect(clientPage.getByTestId('cp-grid')).toBeVisible();
    // No recruiter shell at all.
    await expect(clientPage.locator('nav.sidebar')).toHaveCount(0);
    await expect(clientPage.getByTestId('cp-row')).toHaveCount(3);
    // The contact's address never reaches the client's page.
    await expect(clientPage.locator('body')).not.toContainText(`known-${tag}@e2e.example.com`);

    // And the token opens nothing on our side.
    const clientToken = await clientPage.evaluate(() => localStorage.getItem('juicebox.token'));
    const probe = await request.get('/v1/candidates', {
      headers: { Authorization: `Bearer ${clientToken}` },
    });
    expect(probe.status()).toBe(401);
    summary.client_token_on_crm = probe.status();

    const waiting = clientPage.getByTestId('cp-row').filter({ hasText: 'Waiting Person' });
    await waiting.getByTestId('cp-row-stage').selectOption('first_round');
    await expect(clientPage.getByTestId('cp-pile-first_round')).toContainText('1');
    await waiting.locator('.cp-name').click();
    await clientPage.getByTestId('cp-note-input').fill('Booked for Monday 10am.');
    await clientPage.getByRole('button', { name: 'Add note' }).click();
    await expect(clientPage.getByTestId('cp-note')).toContainText('Booked for Monday 10am.');
    await expect(clientPage.getByTestId('cp-drawer')).toContainText(
      'moved Awaiting review → First round',
    );
    await clientPage.getByRole('button', { name: 'Close' }).click();
    await clientPage.screenshot({ path: path.join(ARTIFACT_DIR, 'client-side.png'), fullPage: true });
    summary.client_moved = 'Waiting Person: awaiting → first_round, with a note';

    // --- back on our side, the move and the note are there ----------------------
    await page.getByRole('button', { name: 'Refresh' }).click();
    const ours = page.getByTestId('cp-row').filter({ hasText: 'Waiting Person' });
    await expect(ours.getByTestId('cp-row-stage')).toHaveValue('first_round');
    await expect(ours).toContainText(`Flowgen ${tag}`); // "moved by" tag
    await expect(ours).toContainText('Booked for Monday 10am.');

    // A bulk move from our side lands on theirs.
    await ours.locator('input[type=checkbox]').check();
    await page.getByTestId('cp-bulk').locator('select').selectOption('second_round');
    await page.getByTestId('cp-bulk').getByRole('button', { name: 'Apply' }).click();
    await expect(ours.getByTestId('cp-row-stage')).toHaveValue('second_round');
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'recruiter-side.png'), fullPage: true });

    await clientPage.getByRole('button', { name: 'Refresh' }).click();
    await expect(
      clientPage
        .getByTestId('cp-row')
        .filter({ hasText: 'Waiting Person' })
        .getByTestId('cp-row-stage'),
    ).toHaveValue('second_round');
    summary.recruiter_bulk_move_visible_to_client = true;

    fs.writeFileSync(
      path.join(ARTIFACT_DIR, 'summary.json'),
      JSON.stringify({ ran_at: new Date().toISOString(), username, ...summary }, null, 2),
    );
    await clientContext.close();
  });
});
