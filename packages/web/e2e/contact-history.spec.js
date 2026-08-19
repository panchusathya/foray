import { expect, test } from '@playwright/test';

import {
  addToProject,
  createOrg,
  createProject,
  createSequence,
  login,
  seedCandidates,
} from './support.js';

/**
 * The candidate detail page and its contact history. Real Message rows come
 * from enrolling + running a sequence (console/memory sender on the test
 * stacks), so what renders is the same data path production uses.
 */
test.describe('contact history', () => {
  async function contacted(request, tag) {
    const org = await createOrg(request, tag);
    const ids = await seedCandidates(request, org, tag);
    const project = await createProject(request, org, `${tag} Role`);
    await addToProject(request, org, project.id, ids);
    const sequence = await createSequence(request, org, project.id);
    await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: [ids[0]], project_id: project.id },
    });
    await request.post(`/v1/sequences/${sequence.id}/run`, { headers: org.headers });
    return { org, ids, project, sequence };
  }

  test('a contacted candidate shows stats and the sent email', async ({ page, request }) => {
    const { org, ids } = await contacted(request, 'hist');

    await login(page, org.email);
    await page.goto(`/contacts/${ids[0]}`);

    const history = page.getByTestId('contact-history');
    await expect(history).toBeVisible();
    // The stats strip has real dates, not em-dashes.
    await expect(history.getByText('first contacted')).toBeVisible();
    await expect(history.getByText('1 / 0')).toBeVisible();

    // The thread opens with the actual sent body.
    await expect(page.getByTestId('history-thread').first()).toBeVisible();
    await expect(page.getByTestId('history-message').first()).toContainText('Hi Ada');
    await expect(page.getByTestId('history-message').first()).toContainText('sequence');
  });

  test('an uncontacted candidate shows a calm empty state', async ({ page, request }) => {
    const org = await createOrg(request, 'hist-empty');
    const ids = await seedCandidates(request, org, 'hist-empty');

    await login(page, org.email);
    await page.goto(`/contacts/${ids[1]}`);

    await expect(page.getByTestId('candidate-profile')).toBeVisible();
    await expect(page.getByTestId('contact-history')).toContainText(
      'No emails with this candidate yet',
    );
  });

  test('every people surface links into the detail page', async ({ page, request }) => {
    const { org, ids, project } = await contacted(request, 'hist-links');

    await login(page, org.email);

    // From Contacts.
    await page.goto('/contacts');
    await page.getByRole('link', { name: 'Ada hist-links' }).first().click();
    await expect(page).toHaveURL(new RegExp(`/contacts/${ids[0]}`));
    await expect(page.getByTestId('contact-history')).toBeVisible();

    // From the pipeline board.
    await page.goto(`/projects/${project.id}`);
    await page.getByRole('link', { name: 'Ada hist-links' }).first().click();
    await expect(page).toHaveURL(new RegExp(`/contacts/${ids[0]}`));
  });

  test('contacts rows badge the last-emailed date', async ({ page, request }) => {
    const { org } = await contacted(request, 'hist-badge');

    await login(page, org.email);
    await page.goto('/contacts');

    await expect(page.getByText(/emailed \d/).first()).toBeVisible();
  });
});
