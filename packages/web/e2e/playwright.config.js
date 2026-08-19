import { defineConfig, devices } from '@playwright/test';

/**
 * Browser regression suite. Runs against a real stack — the staging compose
 * (db + api + web) in CI, or a local dev server — never against production.
 *
 * Workers are pinned to 1: every spec talks to one shared database, and
 * parallel orgs racing on the same pipeline/sequence rows produce failures
 * that say nothing about the code. Specs isolate themselves by signing up
 * their own org instead.
 */
export default defineConfig({
  testDir: '.',
  testMatch: '**/*.spec.js',
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:18080',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 10_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
