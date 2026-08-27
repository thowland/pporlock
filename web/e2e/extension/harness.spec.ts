import { test, expect, chromium, type BrowserContext } from '@playwright/test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

/**
 * Proves the E2E harness can load an unpacked MV3 extension and reach its
 * service worker. Sprint 5 depends on this working; discovering it does not
 * would be expensive there and is cheap here.
 */

const EXT_PATH = resolve(import.meta.dirname, '../../../testfixtures/minimal-extension');

let context: BrowserContext;
let userDataDir: string;

test.beforeAll(async () => {
  userDataDir = mkdtempSync(join(tmpdir(), 'pporlock-e2e-'));
  context = await chromium.launchPersistentContext(userDataDir, {
    // MV3 extensions do not load in headless mode. The extension suite is
    // headed by necessity, which is why it is a separate Playwright project
    // from the web suite (SPEC-3 §11).
    headless: false,
    args: [`--disable-extensions-except=${EXT_PATH}`, `--load-extension=${EXT_PATH}`],
  });
});

test.afterAll(async () => {
  await context?.close();
  rmSync(userDataDir, { recursive: true, force: true });
});

test('loads an unpacked MV3 extension and starts its service worker', async () => {
  let [worker] = context.serviceWorkers();
  if (!worker) {
    worker = await context.waitForEvent('serviceworker', { timeout: 15_000 });
  }
  expect(worker.url()).toContain('sw.js');

  const extensionId = new URL(worker.url()).host;
  expect(extensionId).toMatch(/^[a-p]{32}$/);

  await expect
    .poll(async () => worker.evaluate(() => self.__pporlock_probe === true), { timeout: 10_000 })
    .toBe(true);
});
