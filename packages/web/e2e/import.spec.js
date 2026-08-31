import { expect, test } from '@playwright/test';

import { createOrg, login } from './support.js';

/**
 * Importing a CSV, and the duplicate check in front of it.
 *
 * The gap worth guarding: `upsert_candidates` dedupes on
 * `(org_id, source, source_id)`, so re-uploading the same export merges -- but
 * the same human already in the CRM under a *different* source (Foray
 * sourcing, a hand-added contact, an intake caller) is invisible to it and
 * lands as a second record. `uq_candidate_email_org` looks like it would stop
 * that, and does not: `sync_candidate_emails` skips an address somebody else
 * owns rather than raising, so the duplicate keeps the address in its JSON and
 * gets no projection row.
 *
 * The check is advice, not enforcement -- a flagged row the recruiter re-ticks
 * still imports. What must hold is that the *default* action never does.
 */

const HEADER =
  '"First name","Last name","Location","LinkedIn","Personal Email",'
  + '"Personal Email Verification","Work Email","Work Email Verification","Phone Numbers",'
  + '"Phone Line Type","Phone Verification Status","GitHub","Current Title",'
  + '"Current Company","Education","Total Experience","Current Tenure","Median Tenure",'
  + '"Profile URL"';

function row({ first, last, linkedin = '', email = '', company = '' }) {
  return (
    `"${first}","${last}","San Francisco","${linkedin}","${email}",`
    + `"${email ? 'Deliverable' : ''}","","","","","","","Engineer","${company}",`
    + '"","","","",""'
  );
}

function csv(...rows) {
  return [HEADER, ...rows].join('\n');
}

async function upload(page, text, name = 'export.csv') {
  await page.getByTestId('import-file').setInputFiles({
    name,
    mimeType: 'text/csv',
    buffer: Buffer.from(text, 'utf8'),
  });
}

test.describe('CSV import', () => {
  test('a clean file pre-selects every mailable row and flags nothing', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'imp-clean');
    await login(page, org.email);
    await page.goto('/import');

    await upload(
      page,
      csv(
        row({ first: 'Ada', last: 'Lovelace', email: 'ada@example.com', company: 'Modal' }),
        row({ first: 'Bo', last: 'Diddley', email: 'bo@example.com', company: 'Acme' }),
      ),
    );

    await expect(page.getByTestId('import-row')).toHaveCount(2);
    await expect(page.getByTestId('import-summary')).toContainText('2 with a usable email');
    await expect(page.getByTestId('import-summary')).not.toContainText('already in contacts');
    await expect(page.getByTestId('import-duplicate')).toHaveCount(0);
    await expect(page.getByTestId('import-remove-duplicates')).toHaveCount(0);
    for (const box of await page.getByTestId('import-row').locator('input').all()) {
      await expect(box).toBeChecked();
    }
  });

  test('a row whose address is already a contact is flagged and unticked', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'imp-email');
    // Already on file, and deliberately under another source: this is exactly
    // the row (source, source_id) cannot see.
    await request.post('/v1/candidates/bulk', {
      headers: org.headers,
      data: {
        candidates: [
          {
            source: 'foray',
            source_id: 'gh:adalovelace',
            full_name: 'Ada Lovelace',
            emails: ['ada@example.com'],
          },
        ],
      },
    });

    await login(page, org.email);
    await page.goto('/import');
    await upload(
      page,
      csv(
        row({ first: 'Ada', last: 'Lovelace', email: 'ada@example.com', company: 'Modal' }),
        row({ first: 'Bo', last: 'Diddley', email: 'bo@example.com', company: 'Acme' }),
      ),
    );

    await expect(page.getByTestId('import-duplicate')).toHaveCount(1);
    await expect(page.getByTestId('import-duplicate')).toContainText('Ada Lovelace');
    await expect(page.getByTestId('import-duplicate')).toContainText('same email');
    await expect(page.getByTestId('import-duplicate-note')).toContainText('1 row already');

    // The default action is never the surprising one: Ada is off, Bo is on.
    const rows = page.getByTestId('import-row');
    await expect(rows.nth(0).locator('input')).not.toBeChecked();
    await expect(rows.nth(1).locator('input')).toBeChecked();
    await expect(page.getByRole('button', { name: /^Import 1 and enroll/ })).toBeVisible();
  });

  test('the same LinkedIn profile under a different source is caught', async ({
    page,
    request,
  }) => {
    const org = await createOrg(request, 'imp-li');
    await request.post('/v1/candidates/bulk', {
      headers: org.headers,
      data: {
        candidates: [
          {
            source: 'manual',
            source_id: 'man-1',
            full_name: 'Grace Hopper',
            // A different spelling and a different address than the file's.
            linkedin_url: 'https://linkedin.com/in/grace-hopper',
            emails: ['grace@navy.example.com'],
          },
        ],
      },
    });

    await login(page, org.email);
    await page.goto('/import');
    await upload(
      page,
      csv(
        row({
          first: 'Grace',
          last: 'Hopper',
          linkedin: 'http://www.linkedin.com/in/Grace-Hopper/',
          email: 'grace@personal.example.com',
          company: 'Univac',
        }),
      ),
    );

    await expect(page.getByTestId('import-duplicate')).toContainText('same LinkedIn');
    await expect(page.getByTestId('import-row').locator('input')).not.toBeChecked();
  });

  test('"Remove duplicates" clears them from the selection', async ({ page, request }) => {
    const org = await createOrg(request, 'imp-remove');
    await request.post('/v1/candidates/bulk', {
      headers: org.headers,
      data: {
        candidates: [
          {
            source: 'foray',
            source_id: 'gh:ada',
            full_name: 'Ada Lovelace',
            emails: ['ada@example.com'],
          },
        ],
      },
    });

    await login(page, org.email);
    await page.goto('/import');
    await upload(
      page,
      csv(
        row({ first: 'Ada', last: 'Lovelace', email: 'ada@example.com', company: 'Modal' }),
        row({ first: 'Bo', last: 'Diddley', email: 'bo@example.com', company: 'Acme' }),
      ),
    );

    // Wait for the check to land before re-ticking, or the assertion below
    // races it. (The page itself is safe either way: the check prunes from the
    // live selection rather than replacing it, so a tick made mid-flight is
    // not thrown away -- only an actual duplicate is unticked.)
    const rows = page.getByTestId('import-row');
    await expect(page.getByTestId('import-duplicate')).toHaveCount(1);
    await expect(rows.nth(0).locator('input')).not.toBeChecked();

    // Re-ticking a flagged row is allowed -- a name-and-company match can be
    // two different people, so the recruiter has the last word.
    await rows.nth(0).locator('input').check();
    await expect(page.getByRole('button', { name: /^Import 2 and enroll/ })).toBeVisible();

    await page.getByTestId('import-remove-duplicates').click();
    await expect(rows.nth(0).locator('input')).not.toBeChecked();
    await expect(rows.nth(1).locator('input')).toBeChecked();
    await expect(page.getByRole('button', { name: /^Import 1 and enroll/ })).toBeVisible();
  });

  test('importing only the non-duplicates adds exactly those', async ({ page, request }) => {
    const org = await createOrg(request, 'imp-write');
    await request.post('/v1/candidates/bulk', {
      headers: org.headers,
      data: {
        candidates: [
          {
            source: 'foray',
            source_id: 'gh:ada',
            full_name: 'Ada Lovelace',
            emails: ['ada@example.com'],
          },
        ],
      },
    });

    await login(page, org.email);
    await page.goto('/import');
    await upload(
      page,
      csv(
        row({ first: 'Ada', last: 'Lovelace', email: 'ada@example.com', company: 'Modal' }),
        row({ first: 'Bo', last: 'Diddley', email: 'bo@example.com', company: 'Acme' }),
      ),
    );
    await expect(page.getByTestId('import-duplicate')).toHaveCount(1);

    await page.getByRole('button', { name: /^Import only$/ }).click();
    await expect(page.getByTestId('import-status')).toContainText('Imported 1');
    await expect(page.getByTestId('import-status')).toContainText('1 new, 0 updated');
  });

  test('the flags survive leaving the page and coming back', async ({ page, request }) => {
    // The draft is re-parsed from sessionStorage on mount and the matches are
    // derived, not stored -- so the check has to run again on restore.
    const org = await createOrg(request, 'imp-draft');
    await request.post('/v1/candidates/bulk', {
      headers: org.headers,
      data: {
        candidates: [
          {
            source: 'foray',
            source_id: 'gh:ada',
            full_name: 'Ada Lovelace',
            emails: ['ada@example.com'],
          },
        ],
      },
    });

    await login(page, org.email);
    await page.goto('/import');
    await upload(
      page,
      csv(
        row({ first: 'Ada', last: 'Lovelace', email: 'ada@example.com', company: 'Modal' }),
        row({ first: 'Bo', last: 'Diddley', email: 'bo@example.com', company: 'Acme' }),
      ),
    );
    await expect(page.getByTestId('import-duplicate')).toHaveCount(1);

    await page.getByRole('link', { name: 'Sequences' }).click();
    await expect(page).toHaveURL(/\/sequences$/);
    await page.getByRole('link', { name: 'Import' }).click();

    await expect(page.getByTestId('import-row')).toHaveCount(2);
    await expect(page.getByTestId('import-duplicate')).toHaveCount(1);
    await expect(page.getByTestId('import-row').nth(0).locator('input')).not.toBeChecked();
  });
});
