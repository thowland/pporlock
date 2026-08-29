/* eslint-disable security/detect-non-literal-fs-filename --
   Test harness. Every path is built from a temp directory this file created
   moments earlier, or from the repository root resolved at import time. */
import { expect, test, type Page } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { networkInterfaces, tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { freePorts } from '../extension/ports';

/**
 * REQ PXY-016, end to end against a real daemon.
 *
 * The unit tests stub the API client, so they agree with whatever shape the
 * client believes. That has been wrong three times in this project, and here
 * the cost of being wrong is unusually high: `PUT /exclusions` replaces the
 * whole list and reads `body["entries"]`, so a client sending a bare array
 * would delete all 33 shipped exclusions — every one of which exists because
 * interception breaks that host.
 *
 * So this drives the real button against the real daemon and then asks the
 * daemon what it actually stored.
 */

const REPO = resolve(import.meta.dirname, '../../..');

let daemon: ChildProcess | null = null;
let fixture: ChildProcess | null = null;
let stateDir: string;
let controlPort: number;
let proxyPort: number;
let fixturePort: number;
let token: string;

/** Not loopback: the proxy bypasses it, so a loopback fixture is never seen. */
function lanAddress(): string | null {
  for (const [name, addresses] of Object.entries(networkInterfaces())) {
    if (name.startsWith('lo')) continue;
    for (const address of addresses ?? []) {
      if (address.family === 'IPv4' && !address.internal) return address.address;
    }
  }
  return null;
}

const FIXTURE_HOST = lanAddress();

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

async function exclusions(): Promise<{ pattern: string; source?: string }[]> {
  const body = (await (await api('/exclusions')).json()) as {
    entries: { pattern: string; source?: string }[];
  };
  return body.entries;
}

test.skip(
  FIXTURE_HOST === null,
  'no non-loopback interface: the fixture would be bypassed and no flow would appear',
);

test.beforeAll(async () => {
  test.setTimeout(180_000);
  [proxyPort, controlPort, fixturePort] = (await freePorts(3)) as [number, number, number];
  stateDir = mkdtempSync(join(tmpdir(), 'pporlock-excl-'));
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

  fixture = spawn(
    'uv',
    [
      'run',
      'python',
      '../testfixtures/origin/server.py',
      '--host',
      String(FIXTURE_HOST),
      '--port',
      String(fixturePort),
    ],
    { cwd: join(REPO, 'daemon'), stdio: 'ignore', detached: true },
  );
  await waitFor(`http://${FIXTURE_HOST}:${fixturePort}/health`);

  daemon = spawn('uv', ['run', 'pporlock', '--config', join(stateDir, 'config.yaml'), 'run'], {
    cwd: join(REPO, 'daemon'),
    stdio: 'ignore',
    detached: true,
  });
  await waitFor(`http://127.0.0.1:${controlPort}/state/health`);
  token = readFileSync(join(stateDir, 'token'), 'utf8').trim();

  // One flow to act on.
  await new Promise((done) => {
    const c = spawn('curl', [
      '-s',
      '-o',
      '/dev/null',
      '-x',
      `http://127.0.0.1:${proxyPort}`,
      `http://${FIXTURE_HOST}:${fixturePort}/health`,
    ]);
    c.on('exit', done);
  });
  await new Promise((r) => setTimeout(r, 1500));
});

test.afterAll(async () => {
  for (const child of [daemon, fixture]) {
    if (child?.pid) {
      try {
        process.kill(-child.pid, 'SIGKILL');
      } catch {
        /* already gone */
      }
    }
  }
  rmSync(stateDir, { recursive: true, force: true });
});

async function openUi(page: Page): Promise<void> {
  // The daemon serves the built UI. The token normally arrives by pairing;
  // here it is seeded the way main.tsx stores it after reading the fragment.
  await page.addInitScript((t) => {
    try {
      window.sessionStorage.setItem('pporlock.token', t);
    } catch {
      /* opaque origin */
    }
  }, token);
  await page.goto(`http://127.0.0.1:${controlPort}/#/`, { waitUntil: 'domcontentloaded' });
}

test.describe.configure({ mode: 'serial' });

test('the daemon ships a non-trivial default exclusion list', async () => {
  // The precondition for the test that matters. If this is empty, the
  // regression below could not be detected.
  const entries = await exclusions();
  expect(entries.length).toBeGreaterThan(20);
  expect(entries.every((e) => e.pattern.length > 0)).toBe(true);
});

test('excluding a host from a flow adds exactly one entry and keeps the rest', async ({ page }) => {
  const before = await exclusions();

  await openUi(page);
  const row = page.locator('tbody tr').first();
  await row.waitFor({ state: 'visible', timeout: 20_000 });
  await row.click();

  await page
    .getByRole('button', { name: /exclude/i })
    .first()
    .click();
  // The confirmation states the consequence rather than asking "are you sure".
  await expect(page.getByText(/tunnelled without being decrypted/i)).toBeVisible();
  await page.getByRole('button', { name: 'Exclude this host' }).click();
  await expect(page.getByRole('status')).toContainText(/now excluded/i, { timeout: 15_000 });

  const after = await exclusions();

  // The whole point: PUT replaces the list, so the 33 shipped defaults must
  // still be there. A client sending a bare array would have emptied them.
  expect(after.length).toBe(before.length + 1);
  for (const entry of before) {
    expect(after.find((e) => e.pattern === entry.pattern)).toBeTruthy();
  }
  expect(after.find((e) => e.pattern === FIXTURE_HOST)).toBeTruthy();

  // Defaults keep their provenance rather than being adopted as the user's.
  const defaults = after.filter((e) => e.source === 'default');
  expect(defaults.length).toBe(before.filter((e) => e.source === 'default').length);
});

test('excluding the same host again writes nothing', async ({ page }) => {
  const before = await exclusions();

  await openUi(page);
  const row = page.locator('tbody tr').first();
  await row.waitFor({ state: 'visible', timeout: 20_000 });
  await row.click();
  await page
    .getByRole('button', { name: /exclude/i })
    .first()
    .click();
  // The duplicate check runs on confirm, not on open: it re-reads the list at
  // that moment rather than trusting a copy fetched earlier, which is what
  // stops another client's change being clobbered.
  await page.getByRole('button', { name: 'Exclude this host' }).click();

  // It reports what already covers the host instead of adding a duplicate.
  await expect(page.getByText(/already excluded/i)).toBeVisible({ timeout: 15_000 });

  expect((await exclusions()).length).toBe(before.length);
});
