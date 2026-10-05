import { readFile } from 'node:fs/promises';

import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

/**
 * Getting people out of the CRM, and checking a list against it.
 *
 * Export used to write only the rows on screen -- the grid pages at 100 -- so
 * the case worth pinning is a workspace bigger than a page. The duplicate
 * check's promise is the other way round: it reads the CRM and writes nothing,
 * and the file that comes back is the recruiter's own minus the matches.
 */

async function bulk(request, org, candidates) {
  const response = await request.post('/v1/candidates/bulk', {
    headers: org.headers,
    data: { candidates },
  });
  expect(response.ok()).toBeTruthy();
}

async function downloaded(page, click) {
  const [download] = await Promise.all([page.waitForEvent('download'), click()]);
  return { name: download.suggestedFilename(), text: await readFile(await download.path(), 'utf8') };
}

test.describe('contacts export', () => {
  test('writes every contact in the view, not just the page', async ({ page, request }) => {
    const org = await createOrg(request, 'export-all');
    await bulk(
      request,
      org,
      Array.from({ length: 130 }, (_, n) => ({
        source: 'e2e',
        source_id: `p${n}`,
        full_name: `Person ${String(n).padStart(3, '0')}`,
      })),
    );

    await login(page, org.email);
    await page.goto('/contacts?view=all');
    await expect(page.getByTestId('contacts-grid')).toBeVisible();
    await expect(page.getByTestId('export-all')).toHaveText('Export 130 to CSV');

    const { name, text } = await downloaded(page, () => page.getByTestId('export-all').click());
    const lines = text.replace(/^﻿/, '').trim().split(/\r?\n/);
    expect(name).toMatch(/^contacts-all-\d{4}-\d{2}-\d{2}\.csv$/);
    expect(lines[0]).toContain('Name,First name,Last name');
    expect(lines).toHaveLength(131);
  });
});

test.describe('check for duplicates', () => {
  test('returns the file without the people already in contacts, and imports nothing', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'dupe-check');
    await bulk(request, org, [
      {
        source: 'foray',
        source_id: 'gh:ada',
        full_name: 'Ada Lovelace',
        linkedin_url: 'https://linkedin.com/in/ada-lovelace',
      },
      { source: 'manual', source_id: 'grace', full_name: 'Grace Hopper', emails: ['grace@navy.mil'] },
    ]);

    await login(page, org.email);
    await page.goto('/import?view=duplicates');
    await page.getByTestId('dupe-file').setInputFiles({
      name: 'leads.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(
        'Name,LinkedIn,Email,Notes\n'
          + 'Ada L.,https://www.linkedin.com/in/ada-lovelace/,,"known, by profile"\n'
          + 'Grace,,GRACE@navy.mil,known by address\n'
          + 'Katherine Johnson,https://linkedin.com/in/kj,,new\n',
        'utf8',
      ),
    });

    await expect(page.getByTestId('dupe-row')).toHaveCount(2);
    await expect(page.getByTestId('dupe-download')).toHaveText(/Download 1 without duplicates/i);

    const clean = await downloaded(page, () => page.getByTestId('dupe-download').click());
    expect(clean.name).toBe('leads-deduped.csv');
    expect(clean.text).toBe('Name,LinkedIn,Email,Notes\r\nKatherine Johnson,https://linkedin.com/in/kj,,new\r\n');

    // Keeping one of the matches puts it back in the file.
    await page.getByLabel('Remove row 3').click();
    await expect(page.getByTestId('dupe-download')).toHaveText(/Download 2 without duplicates/i);

    const count = await request.get('/v1/candidates/count', { headers: org.headers });
    expect((await count.json()).count).toBe(2);
  });
});
