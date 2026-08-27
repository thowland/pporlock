import { defineConfig } from '@playwright/test';

/**
 * E2E configuration.
 *
 * Two suites with different needs:
 *   e2e/web/*        headless Chromium against the built web UI
 *   e2e/extension/*  headed persistent context — MV3 extensions cannot be
 *                    loaded headless, so those specs manage their own context
 *                    and ignore baseURL (SPEC-3 §11).
 *
 * From Sprint 4 the web suite runs against the daemon on 127.0.0.1:8081, which
 * serves the built assets (REQ API-003). Until the daemon serves them, `vite
 * preview` stands in.
 */
const PREVIEW_PORT = 4173;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    baseURL: `http://127.0.0.1:${PREVIEW_PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `npx vite preview --port ${PREVIEW_PORT} --strictPort --host 127.0.0.1`,
    url: `http://127.0.0.1:${PREVIEW_PORT}`,
    reuseExistingServer: true,
    timeout: 60_000,
  },
  projects: [
    { name: 'web', testDir: './e2e/web' },
    { name: 'extension', testDir: './e2e/extension' },
  ],
});
