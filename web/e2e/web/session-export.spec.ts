/* eslint-disable security/detect-non-literal-fs-filename --
   Test harness. Every path is built from a temp directory this file created
   moments earlier, or from the repository root resolved at import time. */
import { expect, test, type Page } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { freePorts } from '../extension/ports';

/**
 * Session export, end to end against a real daemon and a real browser.
 * REQ CAP-024, OI-35.
 *
 * The export controls were `<a href download>` for the life of the project and
 * could never have worked: a navigation carries no Authorization header, so the
 * daemon answered 401 and Chrome reported "file was not available on the site".
 * Every unit test passed throughout, because they stubbed the API client and so
 * agreed that a link was enough — and the one component test that touched these
 * controls asserted the anchor's href, pinning the broken mechanism in place.
 *
 * Only a browser can tell you whether a download happens. That is what this is:
 * click the button, catch the download, assert it has bytes and the right name.
 *
 * No proxy traffic is needed — an empty session exports fine, and the bug was
 * in the request, not the contents. Keeping the fixture origin out of it makes
 * this one of the cheaper specs in the suite.
 */

const REPO = resolve(import.meta.dirname, '../../..');

let daemon: ChildProcess | null = null;
let stateDir: string;
let controlPort: number;
let proxyPort: number;
let token: string;
let sessionId: string;

async function waitFor(url: string, tries = 120): Promise<void> {
  for (let i = 0; i < tries; i += 1) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`never came up: ${url}`);
}

function api(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`http://127.0.0.1:${controlPort}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      'X-Pporlock-Client': 'cli',
      ...(init.headers ?? {}),
    },
  });
}

test.beforeAll(async () => {
  test.setTimeout(180_000);
  [proxyPort, controlPort] = (await freePorts(2)) as [number, number];
  stateDir = mkdtempSync(join(tmpdir(), 'pporlock-export-'));
  writeFileSync(
    join(stateDir, 'config.yaml'),
    [
      `state_dir: ${stateDir}`,
      'modules:',
      `  root: ${join(stateDir, 'modules')}`,
      'proxy:',
      `  listen_port: ${proxyPort}`,
      'control:',
      `  listen_port: ${controlPort}`,
      '',
    ].join('\n'),
  );

  daemon = spawn('uv', ['run', 'pporlock', '--config', join(stateDir, 'config.yaml'), 'run'], {
    cwd: join(REPO, 'daemon'),
    stdio: 'ignore',
    detached: true,
  });
  await waitFor(`http://127.0.0.1:${controlPort}/state/health`);
  token = readFileSync(join(stateDir, 'token'), 'utf8').trim();

  // A stopped session is the only precondition: the export controls do not
  // appear while one is still recording.
  const started = (await (
    await api('/sessions', { method: 'POST', body: JSON.stringify({ name: 'export-spec' }) })
  ).json()) as { session_id: string };
  sessionId = started.session_id;
  await api(`/sessions/${sessionId}/stop`, { method: 'POST', body: '{}' });
});

test.afterAll(async () => {
  if (daemon?.pid) {
    try {
      process.kill(-daemon.pid, 'SIGKILL');
    } catch {
      /* already gone */
    }
  }
  rmSync(stateDir, { recursive: true, force: true });
});

async function openSessions(page: Page): Promise<void> {
  await page.addInitScript((t) => {
    try {
      window.sessionStorage.setItem('pporlock.token', t);
    } catch {
      /* opaque origin */
    }
  }, token);
  await page.goto(`http://127.0.0.1:${controlPort}/#/sessions`, { waitUntil: 'domcontentloaded' });
  await page.getByText('export-spec').waitFor({ state: 'visible', timeout: 20_000 });
}

test.describe.configure({ mode: 'serial' });

test('the export route refuses an unauthenticated navigation', async ({ page }) => {
  // The precondition that made this a bug rather than a preference: it is not
  // that a link was untidy, it is that a link cannot work. If this ever returns
  // 200, the export stopped being authenticated and that is its own emergency.
  const response = await page.request.get(
    `http://127.0.0.1:${controlPort}/sessions/${sessionId}/export?format=har`,
  );
  expect(response.status()).toBe(401);
});

for (const [label, suffix] of [
  ['Export', 'pporlock.json'],
  ['Export HAR', 'har.json'],
] as const) {
  test(`${label} downloads a file  # REQ CAP-024`, async ({ page }) => {
    await openSessions(page);

    // Arm the listener before the click: the download can complete first.
    const downloaded = page.waitForEvent('download', { timeout: 20_000 });
    await page.getByRole('button', { name: label, exact: true }).click();

    // A failed export surfaces in the alert region rather than throwing, so
    // race the two — otherwise a regression shows up as a 20s timeout whose
    // message says nothing about what went wrong.
    const alert = page.getByRole('alert');
    const failure = alert
      .waitFor({ state: 'visible', timeout: 20_000 })
      .then(async () => Promise.reject(new Error(`export failed: ${await alert.textContent()}`)));

    const download = await Promise.race([downloaded, failure]);

    expect(download.suggestedFilename()).toContain(suffix);
    const path = await download.path();
    expect(readFileSync(path, 'utf8').length).toBeGreaterThan(0);
  });
}
