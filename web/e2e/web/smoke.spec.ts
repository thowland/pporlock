import { test, expect } from '@playwright/test';

/**
 * Proves the web E2E harness works end to end: built assets are served, the
 * page boots, React mounts, and nothing errors on the console.
 *
 * Sprint 4 replaces this with real assertions against the flow table.
 */

test('serves the built UI and mounts', async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => consoleErrors.push(err.message));

  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'pporlock' })).toBeVisible();
  await expect(page.getByText(/Control origin:/)).toBeVisible();
  expect(consoleErrors).toEqual([]);
});

test('makes no network requests outside the serving origin', async ({ page }) => {
  // SPEC-2 §2.3: the UI is served from loopback and must work offline. No CDN
  // scripts, no external fonts, no telemetry. A dependency that reaches out
  // would break the tool on an air-gapped machine and leak browsing context.
  const foreign: string[] = [];
  page.on('request', (req) => {
    const url = new URL(req.url());
    if (url.protocol === 'data:' || url.protocol === 'blob:') return;
    if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost') {
      foreign.push(req.url());
    }
  });

  await page.goto('/', { waitUntil: 'networkidle' });
  expect(foreign).toEqual([]);
});
