import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

test.describe('intake chat widget', () => {
  test('opens a conversation, sends a message, and survives idempotent retry', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'intake-chat');
    await login(page, org.email);

    const tokenResponse = await request.get('/v1/intake/widget-token', {
      headers: org.headers,
    });
    expect(tokenResponse.ok()).toBeTruthy();
    const { token } = await tokenResponse.json();

    await page.goto(`/intake-chat-test.html?token=${encodeURIComponent(token)}`);
    await expect(page.getByTestId('intake-widget')).toBeVisible();
    await expect(page.getByTestId('intake-widget-messages')).toContainText('Hi');

    const outboundBefore = await page
      .getByTestId('intake-widget-messages')
      .locator('.jb-intake-msg--outbound')
      .count();

    await page.getByTestId('intake-widget-input').fill('Ada Lovelace');
    await page.getByTestId('intake-widget-send').click();
    await expect(page.getByTestId('intake-widget-messages')).toContainText('Ada Lovelace');

    await page.evaluate(async () => {
      const threadId = window.__jbThreadId;
      const threadToken = window.__jbThreadToken;
      const clientMessageId = sessionStorage.getItem(`juicebox-intake-pending:${threadId}`);
      await fetch(`/v1/intake/chat/sessions/${threadId}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token: threadToken,
          body: 'Ada Lovelace',
          client_message_id: clientMessageId,
        }),
      });
    });

    const outboundAfter = await page
      .getByTestId('intake-widget-messages')
      .locator('.jb-intake-msg--outbound')
      .count();
    expect(outboundAfter).toBe(outboundBefore + 1);
  });

  test('shows a single expired message for invalid tokens', async ({ page }) => {
    await page.goto('/intake-chat-test.html?token=org.bad.signature');
    await expect(page.getByTestId('intake-widget-error')).toContainText(
      'This conversation expired',
    );
    await expect(page.getByTestId('intake-widget-input')).toBeDisabled();
  });

  test('shows distinguishable 429 states in the UI', async ({ page, request }) => {
    const org = await createOrg(request, 'intake-chat-429');
    const tokenResponse = await request.get('/v1/intake/widget-token', {
      headers: org.headers,
    });
    const { token } = await tokenResponse.json();

    await page.route('**/v1/intake/chat/sessions', async (route) => {
      if (route.request().method() === 'POST') {
        await route.fulfill({
          status: 429,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: 'Too many new conversations for this organization',
          }),
        });
        return;
      }
      await route.continue();
    });

    await page.goto(`/intake-chat-test.html?token=${encodeURIComponent(token)}`);
    await expect(page.getByTestId('intake-widget-error')).toContainText(
      'Too many new conversations',
    );

    await page.unroute('**/v1/intake/chat/sessions');

    await page.route('**/v1/intake/chat/sessions/*/messages', async (route) => {
      await route.fulfill({
        status: 429,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: 'This conversation has reached its message limit',
        }),
      });
    });

    await page.goto(`/intake-chat-test.html?token=${encodeURIComponent(token)}`);
    await page.getByTestId('intake-widget-input').fill('hello');
    await page.getByTestId('intake-widget-send').click();
    await expect(page.getByTestId('intake-widget-error')).toContainText('message limit');
  });
});
