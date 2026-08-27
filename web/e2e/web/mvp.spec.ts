import { expect, test } from '@playwright/test';

/**
 * The Sprint 4 exit criteria, as tests.
 *
 * These run against the built bundle served by `vite preview`, not the daemon,
 * so they assert the UI's own behaviour: that it boots, that it stays inside its
 * origin, and above all that it never renders an empty table when it is simply
 * not connected. The live-traffic path is demonstrated against the running
 * daemon in the sprint exit demo.
 */

test('boots without console errors', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  page.on('pageerror', (err) => errors.push(err.message));

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.getByText('pporlock', { exact: true })).toBeVisible();
  // Ignore the expected failure to reach a daemon that is not running here.
  expect(errors.filter((e) => !/fetch|network|Failed to load/i.test(e))).toEqual([]);
});

test('says it cannot reach the daemon instead of showing an empty table', async ({ page }) => {
  // REQ WUI-013. The distinction between "nothing is happening" and "we are not
  // connected" must never require inference.
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.banner')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('Cannot reach the daemon.')).toBeVisible();
  await expect(page.getByText('pporlock doctor')).toBeVisible();
  await expect(page.getByText('Not connected to the daemon')).toBeVisible();
});

test('offers the documented filter vocabulary', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.getByLabel('Filter by host')).toBeVisible();
  await expect(page.getByLabel('Filter by path regex')).toBeVisible();
  await expect(page.getByLabel('Filter by status')).toBeVisible();
  await expect(page.getByRole('button', { name: 'modified' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'blocked' })).toBeVisible();
});

test('makes no network requests outside the serving origin', async ({ page }) => {
  // SPEC-2 §2.3: served from loopback, must work offline. No CDN, no fonts, no
  // telemetry — a dependency reaching out would leak browsing context.
  const foreign: string[] = [];
  page.on('request', (req) => {
    const url = new URL(req.url());
    if (url.protocol === 'data:' || url.protocol === 'blob:') return;
    if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost') foreign.push(req.url());
  });

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  expect(foreign).toEqual([]);
});
