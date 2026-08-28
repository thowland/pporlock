/* eslint-disable security/detect-non-literal-fs-filename --
   Test harness. Every path is built from a temp directory this file created
   moments earlier, or from the repository root resolved at import time. */
import { expect, test, chromium, type BrowserContext, type Worker } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, readFileSync } from 'node:fs';
import { networkInterfaces, tmpdir } from 'node:os';
import { freePorts } from './ports';
import { join, resolve } from 'node:path';

/**
 * Sprint 15's exit demo, automated (REQ EXT-020, EXT-021, TST-006).
 *
 * Visit a page whose CSP was relaxed, see a banner naming the responsible
 * module, suppress it for that host, and confirm the modification is still
 * reported everywhere else.
 *
 * The last clause is the point. Suppression silences a warning, not a fact, and
 * a bug that quietly turned it into the latter would be invisible in exactly
 * the situation the banner exists for.
 */

const REPO = resolve(import.meta.dirname, '../../..');
const EXT_PATH = join(REPO, 'extension/dist');

let context: BrowserContext;
let worker: Worker;
let daemon: ChildProcess | null = null;
let fixture: ChildProcess | null = null;
let userDataDir: string;
let stateDir: string;
let extensionId: string;
let token: string;
let proxyPort: number;
let controlPort: number;
let fixturePort: number;

/**
 * The fixture must not be on loopback.
 *
 * The extension's bypass list excludes loopback on purpose: its own control API
 * calls and the fail-safe health check must not route through the proxy, or a
 * dead proxy would be indistinguishable from a dead daemon (SPEC-3 §4.4). A
 * fixture on 127.0.0.1 is therefore never proxied, and this suite would be
 * testing nothing while reporting success.
 *
 * 127.0.0.2 would be ideal — still this machine, but not a bypass entry —
 * except macOS only configures 127.0.0.1 on lo0, and aliasing the rest of
 * 127/8 needs root. So the fixture binds to this host's own LAN address
 * instead: no external network is involved, but Chrome does not bypass it.
 *
 * Sprint 5's fail-safe spec reached for example.com to solve the same problem.
 * This keeps it local, which also means the CSP fixture is available — and
 * "a page whose CSP was relaxed" is the exit demo.
 */
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

/** A module that relaxes CSP, so there is something real to warn about. */
function writeCspModule(): void {
  const dir = join(stateDir, 'modules', 'relax-csp');
  mkdirSync(join(dir, 'assets'), { recursive: true });
  writeFileSync(
    join(dir, 'module.yaml'),
    [
      'name: relax-csp',
      "pporlock_api: '1'",
      'description: Strips CSP so the banner has something to report.',
      'enabled: true',
      'priority: 100',
      'rules:',
      '  - name: strip-csp-everywhere',
      '    action: headers',
      '    match:',
      '      host: "*"',
      '    response:',
      '      remove: [content-security-policy, content-security-policy-report-only]',
      '',
    ].join('\n'),
  );
}

async function startFixture(): Promise<void> {
  fixture = spawn(
    'uv',
    [
      'run',
      'python',
      '../testfixtures/origin/server.py',
      '--host',
      FIXTURE_HOST,
      '--port',
      String(fixturePort),
    ],
    { cwd: join(REPO, 'daemon'), stdio: 'ignore', detached: true },
  );
  for (let i = 0; i < 60; i += 1) {
    try {
      if ((await fetch(`http://${FIXTURE_HOST}:${fixturePort}/health`)).ok) return;
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
    // modules.root is set explicitly because state_dir does not cascade to it
    // (docs/open-issues.md OI-10). Without this the daemon would load the
    // developer's real ~/.pporlock/modules, and this suite would depend on
    // whatever happened to be sitting there.
    [
      `state_dir: ${stateDir}`,
      `modules:`,
      `  root: ${join(stateDir, 'modules')}`,
      `proxy:`,
      `  listen_port: ${proxyPort}`,
      `control:`,
      `  listen_port: ${controlPort}`,
      ``,
    ].join('\n'),
  );
  daemon = spawn('uv', ['run', 'pporlock', '--config', join(stateDir, 'config.yaml'), 'run'], {
    cwd: join(REPO, 'daemon'),
    stdio: 'ignore',
    detached: true,
  });
  for (let i = 0; i < 120; i += 1) {
    try {
      if ((await fetch(`http://127.0.0.1:${controlPort}/state/health`)).ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('daemon did not start');
}

/**
 * One long-lived extension page, used for both messaging and state reads.
 *
 * Two things went wrong before this existed. Reading state through the service
 * worker handle captured in beforeAll breaks when MV3 evicts the worker, and a
 * stale handle reads nothing rather than something old. Opening a fresh page
 * per read instead — inside a poll — churned a page every few hundred
 * milliseconds, which destabilised the worker on its own.
 *
 * So: one page, opened once, reused. It is an extension page, so
 * chrome.runtime and chrome.storage are both available on it, and messaging
 * from a page is what the popup really does anyway.
 */
let extPage: import('@playwright/test').Page;

async function sw<T>(message: unknown): Promise<T> {
  return (await extPage.evaluate(
    async (msg) => (await chrome.runtime.sendMessage(msg)) as unknown,
    message,
  )) as T;
}

async function extState(): Promise<Record<string, unknown>> {
  return (await extPage.evaluate(
    async () => (await chrome.storage.local.get('pporlock.state'))['pporlock.state'] ?? {},
  )) as Record<string, unknown>;
}

test.beforeAll(async () => {
  test.setTimeout(240_000);
  [proxyPort, controlPort, fixturePort] = (await freePorts(3)) as [number, number, number];
  stateDir = mkdtempSync(join(tmpdir(), 'pporlock-banner-state-'));
  userDataDir = mkdtempSync(join(tmpdir(), 'pporlock-banner-chrome-'));

  writeCspModule();
  await startFixture();
  await startDaemon();
  token = readFileSync(join(stateDir, 'token'), 'utf8').trim();

  context = await chromium.launchPersistentContext(userDataDir, {
    headless: false, // MV3 extensions do not load headless.
    ignoreHTTPSErrors: true, // A fresh profile does not inherit keychain trust.
    args: [`--disable-extensions-except=${EXT_PATH}`, `--load-extension=${EXT_PATH}`],
  });

  worker = context.serviceWorkers()[0] ?? (await context.waitForEvent('serviceworker'));
  extensionId = new URL(worker.url()).host;
  extPage = await context.newPage();
  await extPage.goto(`chrome-extension://${extensionId}/src/popup/options.html`);

  await worker.evaluate(
    async ([origin, tok]) => {
      const current = (await chrome.storage.local.get('pporlock.state'))['pporlock.state'] ?? {};
      await chrome.storage.local.set({
        'pporlock.state': { ...current, controlOrigin: origin, token: tok, paired: true },
      });
    },
    [`http://127.0.0.1:${controlPort}`, token] as const,
  );

  const reply = await sw<{ ok: boolean; error?: string }>({ type: 'set_proxy', enabled: true });
  expect(reply.error ?? '').toBe('');
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
  rmSync(userDataDir, { recursive: true, force: true });
  rmSync(stateDir, { recursive: true, force: true });
});

test.describe.configure({ mode: 'serial' });

/**
 * The banner lives in a closed shadow root, so no selector can reach it. That
 * is the security property, and it means the test has to ask the extension
 * whether the host element is present rather than querying for its contents.
 */
async function bannerHostPresent(page: import('@playwright/test').Page): Promise<boolean> {
  return page.evaluate(() => document.getElementById('pporlock-banner-host') !== null);
}

test('the module actually relaxes CSP', async () => {
  // Establish the precondition before asserting on how it is reported. A banner
  // that appears for a modification that did not happen is a different bug.
  const page = await context.newPage();
  await page.goto(`http://${FIXTURE_HOST}:${fixturePort}/csp/nonce`, {
    waitUntil: 'domcontentloaded',
    timeout: 30_000,
  });
  await page.close();

  const flows = (await (
    await fetch(`http://127.0.0.1:${controlPort}/flows?limit=100`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  ).json()) as { flows: { modified?: boolean; provenance?: { notes?: { code: string }[] } }[] };

  const notes = flows.flows.flatMap((f) => f.provenance?.notes ?? []).map((n) => n.code);
  expect(notes).toContain('csp_modified');
});

test('a modified page shows a banner naming the responsible module', async () => {
  const page = await context.newPage();
  await page.goto(`http://${FIXTURE_HOST}:${fixturePort}/csp/nonce`, {
    waitUntil: 'domcontentloaded',
    timeout: 30_000,
  });

  // The worker warns on tab-complete after reading provenance from the daemon,
  // so this is genuinely asynchronous.
  await expect.poll(async () => bannerHostPresent(page), { timeout: 20_000 }).toBe(true);
  await page.close();
});

test('the banner is in a closed shadow root, unreachable from the page', async () => {
  const page = await context.newPage();
  await page.goto(`http://${FIXTURE_HOST}:${fixturePort}/csp/nonce`, {
    waitUntil: 'domcontentloaded',
    timeout: 30_000,
  });
  await expect.poll(async () => bannerHostPresent(page), { timeout: 20_000 }).toBe(true);

  // Page script cannot reach in to read, restyle, or hide it (SPEC-3 §8).
  const reachable = await page.evaluate(
    () => document.getElementById('pporlock-banner-host')?.shadowRoot ?? null,
  );
  expect(reachable).toBeNull();
  await page.close();
});

test('suppressing the host silences the banner', async () => {
  const reply = await sw<{ ok: boolean }>({ type: 'suppress_host', host: FIXTURE_HOST });
  expect(reply.ok).toBe(true);
  await expect
    .poll(async () => (await extState()).suppressedHosts as string[], { timeout: 10_000 })
    .toContain(FIXTURE_HOST);

  const page = await context.newPage();
  await page.goto(`http://${FIXTURE_HOST}:${fixturePort}/csp/nonce`, {
    waitUntil: 'domcontentloaded',
    timeout: 30_000,
  });
  // Give the worker the same window it had to show one, so this is a real
  // absence rather than a race the test happened to win.
  await new Promise((r) => setTimeout(r, 6000));
  expect(await bannerHostPresent(page)).toBe(false);
  await page.close();
});

test('suppression silences the warning, not the fact', async () => {
  // The exit demo's real assertion. If suppressing a host ever stopped the
  // daemon recording the modification, this is where it would show.
  const flows = (await (
    await fetch(`http://127.0.0.1:${controlPort}/flows?limit=100`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  ).json()) as { flows: { provenance?: { notes?: { code: string }[] } }[] };

  const notes = flows.flows.flatMap((f) => f.provenance?.notes ?? []).map((n) => n.code);
  expect(notes).toContain('csp_modified');
});

test('unsuppressing brings it back', async () => {
  await sw({ type: 'unsuppress_host', host: FIXTURE_HOST });
  // Settled before navigating, for the same reason as the other direction:
  // asserting a banner appears while the suppression might still be in place
  // is a coin toss, and it lands wrong under load.
  await expect
    .poll(async () => (await extState()).suppressedHosts as string[], { timeout: 10_000 })
    .not.toContain(FIXTURE_HOST);

  // Reloading rather than a single load: the worker warns on tab-complete
  // after reading provenance from the daemon, and one navigation gives it one
  // chance. A reload gives it another without weakening the assertion — the
  // banner still has to appear.
  const page = await context.newPage();
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.goto(`http://${FIXTURE_HOST}:${fixturePort}/csp/nonce`, {
      waitUntil: 'domcontentloaded',
      timeout: 30_000,
    });
    try {
      await expect.poll(async () => bannerHostPresent(page), { timeout: 8000 }).toBe(true);
      break;
    } catch {
      if (attempt === 2) throw new Error('no banner after three navigations');
    }
  }
  expect(await bannerHostPresent(page)).toBe(true);
  await page.close();
});

test('turning the banner off entirely silences every host', async () => {
  await sw({ type: 'set_banner_enabled', enabled: false });
  // Confirm the setting actually landed before navigating. The reply comes
  // back when the handler returns, but an MV3 worker can be evicted and
  // restarted between messages, and asserting on a banner's absence while the
  // setting might not have persisted tests nothing. This failed only in a full
  // suite run, which is where that timing is worst.
  await expect.poll(async () => (await extState()).bannerEnabled, { timeout: 10_000 }).toBe(false);

  const page = await context.newPage();
  await page.goto(`http://${FIXTURE_HOST}:${fixturePort}/csp/nonce`, {
    waitUntil: 'domcontentloaded',
    timeout: 30_000,
  });
  await new Promise((r) => setTimeout(r, 6000));
  expect(await bannerHostPresent(page)).toBe(false);
  await page.close();

  await sw({ type: 'set_banner_enabled', enabled: true });
  await expect.poll(async () => (await extState()).bannerEnabled, { timeout: 10_000 }).toBe(true);
});
