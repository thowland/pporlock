import { expect, test, chromium, type BrowserContext, type Worker } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { cpSync, mkdtempSync, rmSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { freePorts } from './ports';

/* eslint-disable security/detect-non-literal-fs-filename --
   Test harness: every path is built from a temp directory this file created, or
   from the repository root resolved at import time. */

/**
 * OI-2: tab attribution, measured rather than assumed.
 *
 * The Sprint 6 decision criterion is explicit: if fewer than 95% of flows in a
 * reference browsing session are attributed, the primary mechanism is rejected
 * and the fallback is adopted. This test performs that measurement against a
 * real extension, a real daemon, and real page loads.
 */

const REPO = resolve(import.meta.dirname, '../../..');
const BUILT_EXT = join(REPO, 'extension/dist');

/**
 * A copy of the built extension with `<all_urls>` moved from
 * optional_host_permissions into host_permissions.
 *
 * That is precisely the state after a user grants the optional permission, and
 * it is the only way to reach it in an automated test: chrome.permissions
 * .request needs a user gesture and raises a Chrome-native dialog no driver can
 * dismiss. Nothing else about the extension differs — the code under test is
 * byte-identical to what ships.
 */
function grantedExtension(): string {
  const dir = mkdtempSync(join(tmpdir(), 'pporlock-granted-ext-'));
  cpSync(BUILT_EXT, dir, { recursive: true });

  const manifestPath = join(dir, 'manifest.json');
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as {
    host_permissions: string[];
    optional_host_permissions?: string[];
  };
  manifest.host_permissions = [
    ...manifest.host_permissions,
    ...(manifest.optional_host_permissions ?? []),
  ];
  delete manifest.optional_host_permissions;
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
  return dir;
}

/** The criterion from SPEC-0 §3.6 and implementation-plan.md §4 (S6). */
const COVERAGE_THRESHOLD = 0.95;

let context: BrowserContext;
let worker: Worker;
let daemon: ChildProcess | null = null;
let fixture: ChildProcess | null = null;
let userDataDir: string;
let stateDir: string;
let proxyPort: number;
let controlPort: number;
let fixturePort: number;
let extensionId: string;
let extPath: string;

async function waitFor(url: string, tries = 120): Promise<void> {
  for (let i = 0; i < tries; i += 1) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  throw new Error(`never came up: ${url}`);
}

function token(): string {
  return readFileSync(join(stateDir, 'token'), 'utf8').trim();
}

async function metrics(): Promise<{
  attribution: {
    submitted: number;
    resolved: number;
    attributed: number;
    total: number;
    coverage: number | null;
  };
}> {
  return (await fetch(`http://127.0.0.1:${controlPort}/metrics`, {
    headers: { Authorization: `Bearer ${token()}` },
  }).then((r) => r.json())) as never;
}

test.beforeAll(async () => {
  test.setTimeout(240_000);
  [proxyPort, controlPort, fixturePort] = (await freePorts(3)) as [number, number, number];
  stateDir = mkdtempSync(join(tmpdir(), 'pporlock-attr-state-'));
  userDataDir = mkdtempSync(join(tmpdir(), 'pporlock-attr-chrome-'));

  fixture = spawn(
    'uv',
    ['run', 'python', '../testfixtures/origin/server.py', '--port', String(fixturePort)],
    { cwd: join(REPO, 'daemon'), stdio: 'ignore', detached: true },
  );
  await waitFor(`http://127.0.0.1:${fixturePort}/health`);

  writeFileSync(
    join(stateDir, 'config.yaml'),
    `state_dir: ${stateDir}\nproxy:\n  listen_port: ${proxyPort}\ncontrol:\n  listen_port: ${controlPort}\n`,
  );
  daemon = spawn('uv', ['run', 'pporlock', '--config', join(stateDir, 'config.yaml'), 'run'], {
    cwd: join(REPO, 'daemon'),
    stdio: 'ignore',
    detached: true,
  });
  await waitFor(`http://127.0.0.1:${controlPort}/state/health`);

  extPath = grantedExtension();
  context = await chromium.launchPersistentContext(userDataDir, {
    headless: false,
    ignoreHTTPSErrors: true,
    args: [`--disable-extensions-except=${extPath}`, `--load-extension=${extPath}`],
  });
  worker = context.serviceWorkers()[0] ?? (await context.waitForEvent('serviceworker'));
  extensionId = new URL(worker.url()).host;

  // Point the extension at this daemon, then pair the way a user does: the CLI
  // opens a window, the extension redeems the code. Writing the token straight
  // into storage would skip the handshake that registers the extension's origin
  // with the daemon — and mutating calls would then be refused, which is
  // exactly what happened the first time this test was written.
  await worker.evaluate(async (origin) => {
    await chrome.storage.local.set({ 'pporlock.state': { controlOrigin: origin } });
  }, `http://127.0.0.1:${controlPort}`);

  const begin = (await fetch(`http://127.0.0.1:${controlPort}/pair/begin`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token()}`,
      'Content-Type': 'application/json',
      'X-Pporlock-Client': 'cli',
    },
    body: '{}',
  }).then((r) => r.json())) as { code: string };

  const pairPage = await context.newPage();
  await pairPage.goto(`chrome-extension://${extensionId}/src/popup/options.html`);
  const paired = (await pairPage.evaluate(
    async (code) => (await chrome.runtime.sendMessage({ type: 'pair', code })) as unknown,
    begin.code,
  )) as { ok: boolean; error?: string };
  await pairPage.close();
  if (!paired.ok) throw new Error(`pairing failed: ${paired.error}`);

  // Turn the proxy on through the extension, exactly as a user would.
  const page = await context.newPage();
  await page.goto(`chrome-extension://${extensionId}/src/popup/options.html`);
  const reply = (await page.evaluate(
    async () => (await chrome.runtime.sendMessage({ type: 'set_proxy', enabled: true })) as unknown,
  )) as { ok: boolean; error?: string };
  await page.close();
  if (!reply.ok) throw new Error(`could not enable proxy: ${reply.error}`);
});

test.afterAll(async () => {
  await context?.close();
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
  rmSync(userDataDir, { recursive: true, force: true });
  if (extPath) rmSync(extPath, { recursive: true, force: true });
});

test.describe.configure({ mode: 'serial' });

test('attributes flows to the tab that made them', async () => {
  // Deliberately an external host: the fixture origin is on loopback, which the
  // bypass list excludes on purpose, so loopback traffic never reaches the
  // proxy at all.
  const page = await context.newPage();
  await page.goto('https://example.com', { waitUntil: 'load', timeout: 40_000 });
  await page.waitForTimeout(2500);
  await page.close();

  const flows = (await fetch(`http://127.0.0.1:${controlPort}/flows?limit=200`, {
    headers: { Authorization: `Bearer ${token()}` },
  }).then((r) => r.json())) as { flows: { kind: string; tab_id: number | null }[] };

  const http = flows.flows.filter((f) => f.kind === 'http');
  expect(http.length).toBeGreaterThan(0);
  expect(http.some((f) => f.tab_id !== null)).toBe(true);
});

test('OI-2 DECISION: measures attribution coverage against the 95% criterion', async () => {
  test.setTimeout(180_000);

  // A reference browsing session: several real pages with subresources, which
  // is where attribution either holds up or does not.
  const targets = [
    'https://example.com',
    'https://www.iana.org',
    'https://www.rfc-editor.org',
    'https://example.com/',
  ];

  for (const url of targets) {
    const page = await context.newPage();
    try {
      await page.goto(url, { waitUntil: 'load', timeout: 40_000 });
      await page.waitForTimeout(1500);
    } catch {
      // A slow or unreachable site should not fail the measurement; it just
      // contributes fewer flows.
    }
    await page.close();
  }

  // Attribution is asynchronous by design: the extension batches, the daemon
  // joins, and backfill runs on submission. Give the last batch time to land.
  await page_settle();

  const { attribution } = await metrics();
  const total = attribution.total;

  // eslint-disable-next-line no-console
  console.log(
    `\n=== OI-2 MEASUREMENT ===\n` +
      `  http flows:  ${attribution.total}\n` +
      `  attributed:  ${attribution.attributed}\n` +
      `  observations submitted: ${attribution.submitted}\n` +
      `  coverage:    ${
        attribution.coverage === null ? 'n/a' : `${(attribution.coverage * 100).toFixed(1)}%`
      } (criterion: ${COVERAGE_THRESHOLD * 100}%)\n`,
  );

  expect(total).toBeGreaterThan(10);
  expect(attribution.coverage).not.toBeNull();
  expect(attribution.coverage ?? 0).toBeGreaterThanOrEqual(COVERAGE_THRESHOLD);
});

async function page_settle(): Promise<void> {
  const settle = await context.newPage();
  await settle.waitForTimeout(3000);
  await settle.close();
}
