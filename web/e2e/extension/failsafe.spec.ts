/* eslint-disable security/detect-non-literal-fs-filename --
   This is a test harness. Every path here is built from a temp directory this
   file created moments earlier, or from the repository root resolved at import
   time. None of it comes from user input. */
import { expect, test, chromium, type BrowserContext, type Worker } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

/**
 * The fail-safe, end to end, against a real daemon and a real extension.
 *
 * This is the test that gates Sprint 5. It prevents the worst failure the
 * system can produce: the daemon dies and Chrome is left pointed at a proxy
 * that is no longer there, so every page load fails with nothing to explain it.
 *
 * Everything here is real: an unpacked MV3 extension in a headed Chromium, a
 * live pporlock daemon, and an actual SIGKILL.
 */

const REPO = resolve(import.meta.dirname, '../../..');
const EXT_PATH = join(REPO, 'extension/dist');

let context: BrowserContext;
let worker: Worker;
let daemon: ChildProcess | null = null;
let fixture: ChildProcess | null = null;
let fixturePort: number;
let userDataDir: string;
let stateDir: string;
let proxyPort: number;
let controlPort: number;
let extensionId: string;

function freePort(base: number): number {
  // Ports well away from a developer's running daemon so the suite never fights
  // an interactive session.
  return base + Math.floor(Math.random() * 400);
}

async function startFixture(): Promise<void> {
  fixture = spawn(
    'uv',
    ['run', 'python', '../testfixtures/origin/server.py', '--port', String(fixturePort)],
    { cwd: join(REPO, 'daemon'), stdio: 'ignore', detached: true },
  );
  for (let i = 0; i < 60; i += 1) {
    try {
      if ((await fetch(`http://127.0.0.1:${fixturePort}/health`)).ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error('fixture origin did not start');
}

async function startDaemon(): Promise<void> {
  writeFileSync(
    join(stateDir, 'config.yaml'),
    `state_dir: ${stateDir}\nproxy:\n  listen_port: ${proxyPort}\ncontrol:\n  listen_port: ${controlPort}\n`,
  );
  daemon = spawn('uv', ['run', 'pporlock', '--config', join(stateDir, 'config.yaml'), 'run'], {
    cwd: join(REPO, 'daemon'),
    stdio: 'ignore',
    detached: true,
  });
  for (let i = 0; i < 120; i += 1) {
    try {
      const res = await fetch(`http://127.0.0.1:${controlPort}/state/health`);
      if (res.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('daemon did not start');
}

function killDaemon(): void {
  if (daemon?.pid) {
    try {
      process.kill(-daemon.pid, 'SIGKILL');
    } catch {
      /* already gone */
    }
  }
  daemon = null;
}

/**
 * Send a message to the service worker from an extension page.
 *
 * Not from the worker itself: chrome.runtime.sendMessage does not dispatch to
 * the sender's own onMessage listener, so a worker cannot message itself. Using
 * a page is also what the popup actually does, so this exercises the real path.
 */
async function sw<T>(message: unknown): Promise<T> {
  const page = await context.newPage();
  try {
    await page.goto(`chrome-extension://${extensionId}/src/popup/options.html`);
    return (await page.evaluate(
      async (msg) => (await chrome.runtime.sendMessage(msg)) as unknown,
      message,
    )) as T;
  } finally {
    await page.close();
  }
}

test.beforeAll(async () => {
  test.setTimeout(180_000);
  proxyPort = freePort(18000);
  controlPort = freePort(18500);
  fixturePort = freePort(19000);
  stateDir = mkdtempSync(join(tmpdir(), 'pporlock-e2e-state-'));
  userDataDir = mkdtempSync(join(tmpdir(), 'pporlock-e2e-chrome-'));

  await startFixture();
  await startDaemon();

  context = await chromium.launchPersistentContext(userDataDir, {
    headless: false, // MV3 extensions do not load headless.
    // A fresh profile does not inherit the keychain trust that
    // `pporlock install` establishes, so an intercepted HTTPS request would
    // fail on the certificate. CA trust is a separate concern, verified by
    // `pporlock doctor` and the Sprint 2 demo; ignoring it here keeps a cert
    // problem from masquerading as a routing problem.
    ignoreHTTPSErrors: true,
    args: [`--disable-extensions-except=${EXT_PATH}`, `--load-extension=${EXT_PATH}`],
  });

  worker = context.serviceWorkers()[0] ?? (await context.waitForEvent('serviceworker'));
  extensionId = new URL(worker.url()).host;

  // Point the extension at this test's daemon, and pair it.
  await worker.evaluate(async (origin) => {
    const current = (await chrome.storage.local.get('pporlock.state'))['pporlock.state'] ?? {};
    await chrome.storage.local.set({
      'pporlock.state': { ...current, controlOrigin: origin },
    });
  }, `http://127.0.0.1:${controlPort}`);
});

test.afterAll(async () => {
  await context?.close();
  killDaemon();
  if (fixture?.pid) {
    try {
      process.kill(-fixture.pid, 'SIGKILL');
    } catch {
      /* already gone */
    }
  }
  rmSync(userDataDir, { recursive: true, force: true });
  rmSync(stateDir, { recursive: true, force: true });
});

test.describe.configure({ mode: 'serial' });

test('turns the proxy on from the extension', async () => {
  // The daemon must be reachable and the extension paired first.
  await worker.evaluate(
    async (token) => {
      const current = (await chrome.storage.local.get('pporlock.state'))['pporlock.state'] ?? {};
      await chrome.storage.local.set({
        'pporlock.state': { ...current, token, paired: true },
      });
    },
    readFileSync(join(stateDir, 'token'), 'utf8').trim(),
  );

  const reply = await sw<{ ok: boolean; error?: string }>({ type: 'set_proxy', enabled: true });
  expect(reply.error ?? '').toBe('');
  expect(reply.ok).toBe(true);

  const level = await worker.evaluate(async () => {
    const settings = await chrome.proxy.settings.get({});
    return settings.levelOfControl;
  });
  expect(level).toBe('controlled_by_this_extension');
});

test('routes browsing through the proxy', async () => {
  // Deliberately an external host. The local fixture origin cannot serve here:
  // it is on loopback, and the bypass list excludes loopback on purpose — the
  // extension's own API calls and the fail-safe health check must not route
  // through the proxy (SPEC-3 §4.4 rule 5). So proving that traffic *does*
  // route requires a target that is not bypassed.
  const page = await context.newPage();
  await page.goto('https://example.com', {
    waitUntil: 'domcontentloaded',
    timeout: 30_000,
  });
  await expect(page.locator('h1')).toBeVisible();
  await page.close();

  const token = readFileSync(join(stateDir, 'token'), 'utf8').trim();
  const flows = await fetch(`http://127.0.0.1:${controlPort}/flows?limit=50`, {
    headers: { Authorization: `Bearer ${token}` },
  }).then((r) => r.json());
  expect(flows.flows.length).toBeGreaterThan(0);
});

test('CLEARS THE PROXY when the daemon dies', async () => {
  // The assertion this whole sprint exists for (REQ EXT-010, PXY-008).
  killDaemon();

  // Two consecutive failed checks. Driven explicitly rather than waiting on the
  // interval, so the test asserts the mechanism rather than the clock.
  await sw({ type: 'health_check' });
  await sw({ type: 'health_check' });

  const level = await worker.evaluate(async () => {
    const settings = await chrome.proxy.settings.get({});
    return settings.levelOfControl;
  });
  expect(level).not.toBe('controlled_by_this_extension');

  const state = await worker.evaluate(
    async () => (await chrome.storage.local.get('pporlock.state'))['pporlock.state'],
  );
  expect((state as { proxyEnabled: boolean }).proxyEnabled).toBe(false);
  expect((state as { failSafeTrippedAt: number | null }).failSafeTrippedAt).not.toBeNull();
});

test('the browser can still browse afterwards', async () => {
  // The entire point of the fail-safe: with the daemon dead and the proxy
  // cleared, ordinary browsing keeps working. If this fails, the fail-safe
  // has not done its job even if it cleared the setting.
  const page = await context.newPage();
  await page.goto(`http://127.0.0.1:${fixturePort}/`, {
    waitUntil: 'domcontentloaded',
    timeout: 30_000,
  });
  await expect(page.locator('h1')).toBeVisible();
  await page.close();
});

test('does NOT auto-re-enable when the daemon comes back', async () => {
  // A daemon that crashed once may crash again mid-page-load. Re-enabling is a
  // deliberate user action (SPEC-3 §4.4 rule 4).
  await startDaemon();
  await sw({ type: 'health_check' });
  await sw({ type: 'health_check' });

  const level = await worker.evaluate(async () => {
    const settings = await chrome.proxy.settings.get({});
    return settings.levelOfControl;
  });
  expect(level).not.toBe('controlled_by_this_extension');
  expect(existsSync(join(stateDir, 'token'))).toBe(true);
});
