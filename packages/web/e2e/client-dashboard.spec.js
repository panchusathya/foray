import fs from 'node:fs';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import {
  PASSWORD,
  createOrg,
  createSequence,
  login,
} from './support.js';

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
    // An empty dashboard opens the share box by itself.
    await expect(page.getByTestId('share-form')).toBeVisible();
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
      'moved In process → First round',
    );
    await clientPage.getByRole('button', { name: 'Close' }).click();
    await clientPage.screenshot({ path: path.join(ARTIFACT_DIR, 'client-side.png'), fullPage: true });
    summary.client_moved = 'Waiting Person: in_process → first_round, with a note';

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
  test('share from Contacts takes the role off their sequence; we can rename and edit', async ({
    page,
    browser,
    request,
  }) => {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    const org = await createOrg(request, 'clientedit');
    const tag = Date.now().toString(36);

    const seeded = await request.post('/v1/candidates/bulk', {
      headers: org.headers,
      data: {
        candidates: [
          {
            source: 'e2e',
            source_id: `${tag}-seq`,
            full_name: `Sequenced ${tag}`,
            emails: [`seq-${tag}@e2e.example.com`],
            current_title: 'Staff Engineer',
            current_company: 'Initech',
          },
        ],
      },
    });
    const [candidateId] = (await seeded.json()).ids;
    // The sequence belongs to a project for this client, with its own role.
    const project = await request.post('/v1/projects', {
      headers: org.headers,
      data: { name: `Flowgen ${tag} backend`, role_title: 'Founding Backend Engineer', company: `Flowgen ${tag}` },
    });
    const projectId = (await project.json()).id;
    const sequence = await createSequence(request, org, projectId);
    await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: [candidateId], project_id: projectId },
    });
    const account = await (
      await request.post('/v1/clients', { headers: org.headers, data: { name: `Flowgen ${tag}` } })
    ).json();
    const username = `fge-${tag}`;
    await request.post(`/v1/clients/${account.id}/logins`, {
      headers: org.headers,
      data: { username, password: PASSWORD },
    });

    // --- share from the Contacts bulk bar ---------------------------------------
    await login(page, org.email);
    await page.goto('/contacts?view=all');
    await expect(page.getByTestId('contacts-grid')).toBeVisible();
    await page
      .getByTestId('contact-row')
      .filter({ hasText: `Sequenced ${tag}` })
      .locator('input[type=checkbox]')
      .click();
    await page.getByTestId('bulk-client').selectOption(account.id);
    await page.getByTestId('bulk-share-client').click();
    await expect(page.getByTestId('bulk-result')).toContainText(`1 shared with Flowgen ${tag}`);

    // --- the role came off the sequence, and is editable inline -----------------
    await page.goto(`/clients/${account.id}`);
    const row = page.getByTestId('cp-row').filter({ hasText: `Sequenced ${tag}` });
    const role = row.getByTestId('cp-row-role');
    await expect(role).toHaveValue('Founding Backend Engineer');
    await role.fill('Staff Backend Engineer');
    await role.press('Enter');
    await expect(role).toHaveValue('Staff Backend Engineer');

    // --- edit the details the client sees -----------------------------------------
    await row.locator('.cp-name').click();
    await page.getByTestId('edit-details').click();
    await page.getByTestId('details-current_company').fill('Initech Labs');
    await page.getByRole('button', { name: 'Save details' }).click();
    await expect(page.getByTestId('cp-drawer')).toContainText('Staff Engineer · Initech Labs');
    await page.getByRole('button', { name: 'Close' }).click();

    // --- a CRM edit flows through; the dashboard edit holds ---------------------------
    const patched = await request.patch(`/v1/candidates/${candidateId}`, {
      headers: org.headers,
      data: { current_title: 'Principal Engineer', current_company: 'Initech Global' },
    });
    expect(patched.ok()).toBeTruthy();
    await page.getByRole('button', { name: 'Refresh' }).click();
    // Title came from the CRM; company was edited here, so it stays.
    await expect(row).toContainText('Principal Engineer · Initech Labs');

    // --- rename the client ----------------------------------------------------------
    await page.getByTestId('rename-client').click();
    await page.getByTestId('rename-client-input').fill(`Flowgen AI ${tag}`);
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.locator('.page-header h1')).toContainText(`Flowgen AI ${tag}`);
    await page.screenshot({
      path: path.join(ARTIFACT_DIR, 'recruiter-edits.png'),
      fullPage: true,
    });

    // --- the client sees all three --------------------------------------------------
    const clientContext = await browser.newContext();
    const clientPage = await clientContext.newPage();
    await clientPage.goto('/');
    await clientPage.getByPlaceholder('Email or username').fill(username);
    await clientPage.getByPlaceholder('Password').fill(PASSWORD);
    await clientPage.locator('button[type="submit"]').click();
    await expect(clientPage.locator('.client-header')).toContainText(`Flowgen AI ${tag}`);
    const theirs = clientPage.getByTestId('cp-row').filter({ hasText: `Sequenced ${tag}` });
    await expect(theirs).toContainText('Staff Backend Engineer');
    await expect(theirs).toContainText('Principal Engineer · Initech Labs');
    // The role is ours to set; on their side it is text, not a box.
    await expect(theirs.getByTestId('cp-row-role')).toHaveCount(0);
    await clientPage.screenshot({
      path: path.join(ARTIFACT_DIR, 'client-after-edits.png'),
      fullPage: true,
    });
    await clientContext.close();
  });
  test('on a phone the client gets cards, a working stage picker and a full-screen drawer', async ({
    browser,
    request,
  }) => {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    const org = await createOrg(request, 'clientmobile');
    const tag = Date.now().toString(36);
    const account = await (
      await request.post('/v1/clients', { headers: org.headers, data: { name: `Flowgen ${tag}` } })
    ).json();
    const username = `fgm-${tag}`;
    await request.post(`/v1/clients/${account.id}/logins`, {
      headers: org.headers,
      data: { username, password: PASSWORD },
    });
    const shared = await request.post(`/v1/clients/${account.id}/submissions`, {
      headers: org.headers,
      data: {
        role_title: 'Founding Engineer',
        entries: [
          { full_name: 'Ada Phone', current_title: 'Staff Engineer', current_company: 'Initech' },
          { full_name: 'Grace Phone', stage: 'first_round' },
          { full_name: 'Linus Phone', stage: 'rejected' },
        ],
      },
    });
    const firstId = (await shared.json()).results[0].submission_id;
    await request.post(`/v1/clients/submissions/${firstId}/notes`, {
      headers: org.headers,
      data: { body: 'Free for a first call any afternoon next week.' },
    });

    const phone = await browser.newContext({
      viewport: { width: 390, height: 844 },
      deviceScaleFactor: 2,
      isMobile: true,
      hasTouch: true,
    });
    const page = await phone.newPage();
    await page.goto('/');

    // The eye button shows the password and hides it again.
    await page.getByPlaceholder('Email or username').fill(username);
    const password = page.getByPlaceholder('Password');
    await password.fill(PASSWORD);
    await expect(password).toHaveAttribute('type', 'password');
    await page.getByTestId('password-toggle').click();
    await expect(password).toHaveAttribute('type', 'text');
    await page.getByTestId('password-toggle').click();
    await expect(password).toHaveAttribute('type', 'password');
    await page.locator('button[type="submit"]').click();

    await expect(page.getByTestId('cp-row')).toHaveCount(3);
    // Nothing wider than the screen: no sideways scrolling of the page.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    // Rows are cards, not a squeezed table.
    await expect(page.getByTestId('cp-grid').locator('thead')).toBeHidden();
    const ada = page.getByTestId('cp-row').filter({ hasText: 'Ada Phone' });
    const box = await ada.boundingBox();
    expect(box.width).toBeGreaterThan(330);
    await expect(ada).toContainText('Free for a first call');
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'phone-grid.png'), fullPage: true });

    await ada.getByTestId('cp-row-stage').selectOption('second_round');
    await expect(page.getByTestId('cp-pile-second_round')).toContainText('1');

    await ada.locator('.cp-name').click();
    const drawer = page.getByTestId('cp-drawer');
    await expect(drawer).toBeVisible();
    expect((await drawer.boundingBox()).width).toBeGreaterThanOrEqual(389);
    await expect(drawer).toContainText('moved In process → Second round');
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'phone-drawer.png') });
    await phone.close();
  });
  async function sharedWorkspace(request, prefix) {
    const org = await createOrg(request, prefix);
    const tag = Date.now().toString(36);
    const account = await (
      await request.post('/v1/clients', { headers: org.headers, data: { name: `Flowgen ${tag}` } })
    ).json();
    const username = `${prefix.slice(0, 6)}-${tag}`;
    await request.post(`/v1/clients/${account.id}/logins`, {
      headers: org.headers,
      data: { username, password: PASSWORD },
    });
    const shared = await request.post(`/v1/clients/${account.id}/submissions`, {
      headers: org.headers,
      data: {
        role_title: 'Founding Engineer',
        entries: [
          { full_name: 'Ada Wide', current_title: 'Staff Engineer', current_company: 'Initech' },
          { full_name: 'Grace Wide', stage: 'first_round' },
        ],
      },
    });
    const firstId = (await shared.json()).results[0].submission_id;
    await request.post(`/v1/clients/submissions/${firstId}/notes`, {
      headers: org.headers,
      data: { body: 'The note that lives in the far-right column.' },
    });
    return { org, account, username };
  }

  /** Scroll the grid to its right edge; the name must stay put and uncovered. */
  async function scrollAcross(page) {
    const wrap = page.locator('.cp-grid-wrap');
    const sizes = await wrap.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
    expect(sizes.scroll).toBeGreaterThan(sizes.client);
    const row = page.getByTestId('cp-row').filter({ hasText: 'Ada Wide' });
    const nameBefore = await row.locator('.cp-td-name').boundingBox();
    await wrap.evaluate((el) => {
      el.scrollLeft = el.scrollWidth;
    });
    const nameAfter = await row.locator('.cp-td-name').boundingBox();
    expect(Math.abs(nameAfter.x - nameBefore.x)).toBeLessThan(1); // pinned
    const note = await row.locator('.cp-td-note').boundingBox();
    const wrapBox = await wrap.boundingBox();
    expect(note.x + note.width).toBeLessThanOrEqual(wrapBox.x + wrapBox.width + 1); // reached
    // Nothing scrolled underneath is drawn over the pinned name.
    for (const cell of ['.cp-td-role', '.cp-td-current', '.cp-td-stage']) {
      const box = await row.locator(cell).boundingBox();
      const onTop = await page.evaluate(
        ([x, y]) => document.elementFromPoint(x, y)?.closest('td')?.className ?? '',
        [nameAfter.x + 8, nameAfter.y + nameAfter.height / 2],
      );
      expect(onTop).toContain('cp-td-name');
      expect(box).toBeTruthy();
    }
  }

  test('on a small laptop the grid scrolls across with the name pinned, on both sides', async ({
    browser,
    request,
  }) => {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    const { org, account, username } = await sharedWorkspace(request, 'clientwide');
    const laptop = { viewport: { width: 1100, height: 760 } };

    const client = await browser.newContext(laptop);
    const clientPage = await client.newPage();
    await clientPage.goto('/');
    await clientPage.getByPlaceholder('Email or username').fill(username);
    await clientPage.getByPlaceholder('Password').fill(PASSWORD);
    await clientPage.locator('button[type="submit"]').click();
    await expect(clientPage.getByTestId('cp-row')).toHaveCount(2);
    await scrollAcross(clientPage);
    await clientPage.screenshot({ path: path.join(ARTIFACT_DIR, 'laptop-client-scrolled.png') });
    await client.close();

    const ours = await browser.newContext(laptop);
    const page = await ours.newPage();
    await login(page, org.email);
    await page.goto(`/clients/${account.id}`);
    await expect(page.getByTestId('cp-row')).toHaveCount(2);
    await scrollAcross(page);
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'laptop-recruiter-scrolled.png') });
    await ours.close();
  });

  test('a recruiter on a phone gets a menu, and the client pages fit the screen', async ({
    browser,
    request,
  }) => {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    const { org, account } = await sharedWorkspace(request, 'recphone');
    const phone = await browser.newContext({
      viewport: { width: 390, height: 844 },
      deviceScaleFactor: 2,
      isMobile: true,
      hasTouch: true,
    });
    const page = await phone.newPage();
    await login(page, org.email);

    const noSidewaysScroll = async () =>
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        ),
      ).toBeLessThanOrEqual(0);

    // The sidebar is a bar: links hidden until the menu opens.
    const clientsLink = page.getByRole('link', { name: 'Client dashboards' });
    await expect(clientsLink).toBeHidden();
    await page.getByTestId('sidebar-menu').click();
    await expect(clientsLink).toBeVisible();
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'phone-recruiter-menu.png') });
    await clientsLink.click();
    // Navigating closes the menu.
    await expect(clientsLink).toBeHidden();
    await expect(page.getByTestId('client-card')).toBeVisible();
    await noSidewaysScroll();

    await page.getByTestId('client-card').click();
    await expect(page.getByTestId('cp-row')).toHaveCount(2);
    await noSidewaysScroll();
    await expect(page.getByTestId('cp-grid').locator('thead')).toBeHidden();
    await page.getByRole('button', { name: 'Share candidates' }).click();
    await page.getByTestId('share-input').fill('Linus Phone, 2nd round');
    await page.getByRole('button', { name: 'Share', exact: true }).click();
    await expect(page.getByTestId('share-result')).toContainText('1 added');
    await expect(page.getByTestId('cp-row')).toHaveCount(3);
    await noSidewaysScroll();
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'phone-recruiter-client.png'), fullPage: true });
    expect(account.id).toBeTruthy();
    await phone.close();
  });
});
