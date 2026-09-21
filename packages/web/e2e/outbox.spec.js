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
 * Outbox and the org sending kill switch.
 *
 * Populated in-flight rows need queued send jobs the browser cannot seed, so
 * those cases live in packages/core/tests/test_outbox.py. This covers the
 * surfaces a recruiter hits with an empty org: the nav entry, the empty
 * page, and the Settings toggle that paints the banner -- plus the one flow
 * that needs no job at all: stopping a scheduled row and re-enrolling it.
 */
test.describe('outbox', () => {
  test('a stopped row waits under Cancelled and can be re-enrolled', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'outbox-reenrol');
    const ids = await seedCandidates(request, org, 'reenrol');
    const project = await createProject(request, org, 'Reenrol Role');
    await addToProject(request, org, project.id, ids);
    // A day's delay keeps the row *scheduled* rather than due, so the worker
    // cannot send it out from under the assertions.
    const sequence = await createSequence(request, org, project.id, {
      steps: [
        {
          delay_days: 1,
          subject_template: 'Quick note for {{ first_name }}',
          body_template: 'Hi {{ first_name }}, about {{ role_title | the role }}.',
        },
      ],
    });
    await request.post(`/v1/sequences/${sequence.id}/enroll`, {
      headers: org.headers,
      data: { candidate_ids: [ids[0]] },
    });
    const listed = await (
      await request.get('/v1/outreach/in-flight', { headers: org.headers })
    ).json();
    expect(listed.total).toBe(1);
    await request.post('/v1/outreach/in-flight/cancel', {
      headers: org.headers,
      data: { enrollment_ids: [listed.rows[0].enrollment_id], action: 'stop' },
    });

    await login(page, org.email);
    await page.goto('/outbox');

    // The live view is empty, but says where the stopped row went.
    await expect(page.getByText('Nothing in flight.')).toBeVisible();
    const hint = page.getByTestId('outbox-cancelled-hint');
    await expect(hint).toHaveText('1 cancelled — re-enrol');
    await hint.click();

    const grid = page.getByTestId('outbox-grid');
    await expect(grid).toContainText('Ada reenrol');
    await expect(grid).toContainText('Cancelled');
    await page.getByTestId('outbox-row-reenrol').click();

    // The default answer needs no input: the step's usual send time.
    const panel = page.getByTestId('reenrol-panel');
    await expect(panel.getByTestId('reenrol-when-next_slot')).toBeChecked();
    await panel.getByTestId('reenrol-submit').click();

    await expect(page.getByTestId('outbox-result')).toContainText('Re-enrolled 1');
    await expect(page.getByText(/Nothing cancelled/)).toBeVisible();
    await expect(page.getByTestId('outbox-cancelled-hint')).toHaveCount(0);

    // Back on the live view the person is in flight again, no longer cancelled.
    await page.getByTestId('outbox-state').selectOption('');
    await expect(page.getByTestId('outbox-grid')).toContainText('Ada reenrol');
    await expect(page.getByTestId('outbox-grid')).not.toContainText('Cancelled');

    const after = await (
      await request.get(`/v1/sequences/${sequence.id}/enrollments`, { headers: org.headers })
    ).json();
    expect(after[0].status).toBe('active');
    expect(after[0].stopped_reason).toBe('');
  });

  test('the nav lists Outbox with the other org-wide placement pages', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'outbox-nav');
    await login(page, org.email);
    await page.goto('/');

    const group = page.locator('.sidebar-section', { hasText: 'PLACE PEOPLE' });
    await expect(group.getByRole('link', { name: 'Outbox', exact: true })).toBeVisible();
    await group.getByRole('link', { name: 'Outbox', exact: true }).click();
    await expect(page).toHaveURL(/\/outbox$/);
    await expect(page.getByRole('heading', { name: 'Outbox' })).toBeVisible();
    await expect(page.getByText('Nothing in flight.')).toBeVisible();
  });

  test('the kill switch pauses sending and the banner appears', async ({ page, request }) => {
    const org = await createOrg(request, 'outbox-kill');
    await login(page, org.email);
    await page.goto('/settings');

    const toggle = page.getByTestId('org-sending-toggle');
    await expect(toggle).toHaveText('Sending: on');
    await expect(page.getByTestId('sending-paused-banner')).toHaveCount(0);

    page.once('dialog', (dialog) => dialog.accept());
    await toggle.click();
    await expect(toggle).toHaveText('Sending: off');
    await expect(page.getByTestId('sending-paused-banner')).toBeVisible();
    await expect(page.getByTestId('sending-paused-banner')).toContainText('Sending paused');

    await toggle.click();
    await expect(toggle).toHaveText('Sending: on');
    await expect(page.getByTestId('sending-paused-banner')).toHaveCount(0);
  });
});
