/* eslint-disable security/detect-non-literal-fs-filename --
   Test harness. Every path is built from a temp directory this file created
   moments earlier, or from the repository root resolved at import time. */
import { expect, test, type Page } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { networkInterfaces, tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { freePorts } from '../extension/ports';

/**
 * Module settings (OI-31), end to end against a real daemon and real traffic.
 *
 * The unit tests stub the API client, so they agree with whatever the client
 * believes. This asks the daemon what it actually stored, and asks the *origin*
 * what header it actually received — the only question that matters, and the one
 * no unit test in either language can answer.
 *
 * It exists because of OI-30: the module report shipped with three passing tests
 * and could not have worked on the first click. A settings form has the same
 * shape of risk — a control that renders correctly and changes nothing.
 *
 * `user-agent-switcher` is the subject because it is the shipped example whose
 * whole behaviour is driven from `ctx.config`, so "did the setting take effect"
 * is answerable by reading one request header.
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

/** What the module's settings actually hold, according to the daemon. */
async function config(): Promise<Record<string, unknown>> {
  const body = (await (await api('/modules/user-agent-switcher')).json()) as {
    config: Record<string, unknown>;
  };
  return body.config;
}

/**
 * The `User-Agent` the origin saw, for one request through the proxy.
 *
 * Through `curl` rather than `fetch` so it crosses the proxy the way a browser
 * would; the fixture's header-echo endpoint answers with what it received.
 */
async function userAgentSeenByOrigin(): Promise<string> {
  const output = await new Promise<string>((done) => {
    const child = spawn('curl', [
      '-s',
      '-x',
      `http://127.0.0.1:${proxyPort}`,
      '-A',
      'Mozilla/5.0 (Macintosh) Chrome/125.0.0.0 Safari/537.36',
      '-H',
      'sec-fetch-dest: document',
      `http://${FIXTURE_HOST}:${fixturePort}/echo/headers`,
    ]);
    let out = '';
    child.stdout.on('data', (chunk: Buffer) => (out += chunk.toString()));
    child.on('exit', () => done(out));
  });
  const headers = JSON.parse(output) as Record<string, string>;
  return headers['user-agent'] ?? headers['User-Agent'] ?? '';
}

test.skip(
  FIXTURE_HOST === null,
  'no non-loopback interface: the fixture would be bypassed and no flow would appear',
);

test.beforeAll(async () => {
  test.setTimeout(180_000);
  [proxyPort, controlPort, fixturePort] = (await freePorts(3)) as [number, number, number];
  stateDir = mkdtempSync(join(tmpdir(), 'pporlock-modset-'));
  const modules = join(stateDir, 'modules');
  mkdirSync(modules, { recursive: true });
  // The shipped example, copied rather than written inline: the point is that
  // the module people are given works, not that a purpose-built one does.
  cpSync(join(REPO, 'examples/modules/user-agent-switcher'), join(modules, 'user-agent-switcher'), {
    recursive: true,
  });

  writeFileSync(
    join(stateDir, 'config.yaml'),
    [
      `state_dir: ${stateDir}`,
      'modules:',
      `  root: ${modules}`,
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

  // Scoped at the fixture and turned on. Enabling is deliberately a separate
  // act from installing (REQ MOD-003), which is why it happens here.
  await api('/modules/user-agent-switcher', {
    method: 'PATCH',
    body: JSON.stringify({ config: { hosts: [String(FIXTURE_HOST)] } }),
  });
  await api('/modules/user-agent-switcher', {
    method: 'PATCH',
    body: JSON.stringify({ enabled: true }),
  });
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

async function openModules(page: Page): Promise<void> {
  // The daemon serves the built UI. The token normally arrives by pairing;
  // here it is seeded the way main.tsx stores it after reading the fragment.
  await page.addInitScript((t) => {
    try {
      window.sessionStorage.setItem('pporlock.token', t);
    } catch {
      /* opaque origin */
    }
  }, token);
  await page.goto(`http://127.0.0.1:${controlPort}/#/modules`, { waitUntil: 'domcontentloaded' });
}

test.describe.configure({ mode: 'serial' });

test('the shipped module reaches the browser with a settings control', async ({ page }) => {
  await openModules(page);
  // Exact: an accessible name is matched as a substring by default, and
  // "Move user-agent-switcher later" contains this one.
  await expect(page.getByRole('button', { name: 'user-agent-switcher', exact: true })).toBeVisible({
    timeout: 20_000,
  });
  await expect(
    page.getByRole('button', { name: 'Settings for user-agent-switcher' }),
  ).toBeVisible();
});

test('the form renders the fields the module declared, at their current values', async ({
  page,
}) => {
  await openModules(page);
  await page.getByRole('button', { name: 'Settings for user-agent-switcher' }).click();

  // One control per declared field. The UI is rendering the daemon's
  // declaration; it knows nothing about crawlers.
  await expect(page.getByLabel('Identify as')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByLabel('Hosts')).toHaveValue(String(FIXTURE_HOST));
  await expect(page.getByLabel('Remove Chrome client hints')).toBeChecked();

  // REQ MOD-031 — a surface that changes what module code does is an
  // authoring surface, and every one of them says the code is unsandboxed.
  await expect(page.getByText(/unsandboxed/i)).toBeVisible();
});

test('saving a setting changes the next request the origin sees', async ({ page }) => {
  // The question no unit test on either side can answer.
  expect(await userAgentSeenByOrigin()).toContain('Googlebot/2.1');

  await openModules(page);
  await page.getByRole('button', { name: 'Settings for user-agent-switcher' }).click();
  await page.getByLabel('Identify as').selectOption('claudebot');
  await page.getByRole('button', { name: 'Save' }).click();

  await expect
    .poll(async () => await userAgentSeenByOrigin(), { timeout: 20_000 })
    .toContain('ClaudeBot/1.0');
});

test('only the fields the user changed are stored', async ({ page }) => {
  /*
   * Storing the whole form would freeze today's defaults into the user's state,
   * so a later version of the module that improved one would never reach anyone
   * who had opened this dialog once. The sidecar is the proof.
   */
  await openModules(page);
  await page.getByRole('button', { name: 'Settings for user-agent-switcher' }).click();
  await page.getByLabel('Identify as').selectOption('gptbot');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(
    page.getByRole('button', { name: 'Settings for user-agent-switcher' }),
  ).toBeVisible();

  const stored = JSON.parse(readFileSync(join(stateDir, 'module-state.json'), 'utf8')) as Record<
    string,
    { config?: Record<string, unknown> }
  >;
  const overrides = stored['user-agent-switcher']?.config ?? {};

  expect(Object.keys(overrides).sort()).toEqual(['hosts', 'identity']);
  // Untouched fields are absent from the sidecar and still in force.
  expect(await config()).toMatchObject({ identity: 'gptbot', strip_client_hints: true });
});

test('a value the module would refuse is refused, and the form stays open', async ({ page }) => {
  // Driven through the API rather than the select, because the select cannot
  // offer an invalid value — which is the point: the daemon does not rely on
  // the form having been honest.
  const response = await api('/modules/user-agent-switcher', {
    method: 'PATCH',
    body: JSON.stringify({ config: { identity: 'not-a-crawler' } }),
  });
  expect(response.status).toBe(400);

  // And nothing was written: the previous test's value still stands.
  expect(await config()).toMatchObject({ identity: 'gptbot' });

  await openModules(page);
  await page.getByRole('button', { name: 'Settings for user-agent-switcher' }).click();
  await expect(page.getByLabel('Identify as')).toHaveValue('gptbot', { timeout: 20_000 });
});
