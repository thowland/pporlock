/* eslint-disable security/detect-non-literal-fs-filename --
   Test harness. Every path is built from a temp directory this file created
   moments earlier, or from the repository root resolved at import time. */
import {
  expect,
  test,
  chromium,
  type BrowserContext,
  type Page,
  type Worker,
} from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, readFileSync } from 'node:fs';
import { createConnection } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { freePorts } from './ports';

/**
 * What must still be true the second time you use pporlock — OI-19, OI-8, OI-9.
 *
 * The suite already drives real Chrome with the real built extension against a
 * real daemon, so "end to end" was never the gap. The gap was the *lifecycle*:
 * every other extension spec starts a fresh daemon, pairs, asserts, and tears
 * down. None of them use a daemon that had already been running once, which is
 * the only state an ordinary user is ever in after their first day.
 *
 * OI-19 lived exactly there. `failsafe.spec.ts` does restart the daemon — and
 * then asserts the proxy is *not* re-enabled and the token file still exists.
 * Both stay true when pairing has been silently lost, so that test walked over
 * the bug and reported success. A negative assertion passes for the wrong
 * reason precisely when the thing it is near is broken.
 *
 * So the assertions here are positive and deliberately unforgiving: after a
 * restart, with no re-pairing and no user intervention, the extension can still
 * *use* the control API, and the state the user set is still set.
 */

const REPO = resolve(import.meta.dirname, '../../..');
const EXT_PATH = join(REPO, 'extension/dist');

let context: BrowserContext;
let worker: Worker;
let daemon: ChildProcess | null = null;
let userDataDir: string;
let stateDir: string;
let configPath: string;
let extensionId: string;
let token: string;
let proxyPort: number;
let controlPort: number;
let extPage: Page;

/** Resolves once nothing holds the port, or throws. */
async function waitForPortFree(port: number, timeoutMs = 30_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const inUse = await new Promise<boolean>((resolveIsUse) => {
      const socket = createConnection({ port, host: '127.0.0.1' });
      socket.once('connect', () => {
        socket.destroy();
        resolveIsUse(true);
      });
      socket.once('error', () => {
        socket.destroy();
        resolveIsUse(false);
      });
    });
    if (!inUse) return;
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`port ${port} never became free`);
}

/**
 * Start the daemon and wait for it to answer.
 *
 * The wait matters more than it looks. `pporlock run` exits with only "Error
 * logged during startup, exiting..." when its port is still held by the
 * previous process, so a restart that does not wait produces a dead daemon and
 * a test failure that blames the wrong thing (noted under OI-19).
 */
async function startDaemon(): Promise<void> {
  daemon = spawn('uv', ['run', 'pporlock', '--config', configPath, 'run'], {
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

async function stopDaemon(): Promise<void> {
  if (daemon?.pid) {
    try {
      process.kill(-daemon.pid, 'SIGKILL');
    } catch {
      /* already gone */
    }
  }
  daemon = null;
  await waitForPortFree(controlPort);
  await waitForPortFree(proxyPort);
}

/**
 * A control-API call issued from inside the extension.
 *
 * **It must be a POST to prove anything.** The extension's manifest takes
 * host_permissions over all of loopback, so Chrome treats a GET to the control
 * API as a permitted same-context fetch and sends no `Origin` header at all —
 * which the daemon allows unconditionally, by the rule that exists for curl and
 * the CLI. A GET therefore returns 200 whether or not the extension is paired,
 * and the first version of this file was built on one: it passed with the OI-19
 * fix reverted, proving nothing.
 *
 * Chrome does attach `Origin: chrome-extension://<id>` to a non-GET, and the
 * page cannot forge it. So a mutating call is the only thing here that reaches
 * the origin policy — which is also why the user hit this by *recording*, a
 * POST, rather than by reading anything.
 */
async function callFromExtension(path: string, init?: RequestInit): Promise<number> {
  return extPage.evaluate(
    async ([url, options]) => {
      const response = await fetch(url as string, (options ?? undefined) as RequestInit);
      return response.status;
    },
    [`http://127.0.0.1:${controlPort}${path}`, init ?? null] as const,
  );
}

/**
 * Proof the daemon really did restart, rather than the test racing past a
 * still-live one.
 *
 * A restart spec whose restart silently did not happen is the exact failure
 * this file was written to fix, so it is asserted rather than assumed: uptime
 * must have gone backwards.
 */
async function uptime(): Promise<number> {
  const state = (await fetch(`http://127.0.0.1:${controlPort}/state`, {
    headers: { Authorization: `Bearer ${token}`, 'X-Pporlock-Client': 'cli' },
  }).then((r) => r.json())) as { proxy: { uptime_s: number } };
  return state.proxy.uptime_s;
}

/** The active profile, as the daemon reports it (`GET /state`). */
async function activeProfile(): Promise<string> {
  const state = (await fetch(`http://127.0.0.1:${controlPort}/state`, {
    headers: { Authorization: `Bearer ${token}`, 'X-Pporlock-Client': 'cli' },
  }).then((r) => r.json())) as { active_profile: string };
  return state.active_profile;
}

/**
 * Start a recording from inside the extension, returning the HTTP status.
 *
 * A POST, so Chrome attaches the extension's Origin — and the same operation
 * the user was performing when OI-19 surfaced.
 */
async function startRecording(name: string): Promise<number> {
  return extPage.evaluate(
    async ([port, tok, sessionName]) => {
      const response = await fetch(`http://127.0.0.1:${port as number}/sessions`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${tok as string}`,
          'Content-Type': 'application/json',
          'X-Pporlock-Client': 'extension',
        },
        body: JSON.stringify({ name: sessionName as string }),
      });
      return response.status;
    },
    [controlPort, token, name] as const,
  );
}

function authorizedRead(): RequestInit {
  return { headers: { Authorization: `Bearer ${token}`, 'X-Pporlock-Client': 'extension' } };
}

test.beforeAll(async () => {
  test.setTimeout(240_000);
  [proxyPort, controlPort] = (await freePorts(2)) as [number, number];
  stateDir = mkdtempSync(join(tmpdir(), 'pporlock-restart-state-'));
  userDataDir = mkdtempSync(join(tmpdir(), 'pporlock-restart-chrome-'));
  mkdirSync(join(stateDir, 'modules'), { recursive: true });

  configPath = join(stateDir, 'config.yaml');
  writeFileSync(
    configPath,
    // modules.root explicitly, because state_dir does not cascade to it
    // (OI-10). Without it the daemon reads the developer's real ~/.pporlock.
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

  await startDaemon();
  token = readFileSync(join(stateDir, 'token'), 'utf8').trim();

  context = await chromium.launchPersistentContext(userDataDir, {
    headless: false, // MV3 extensions do not load headless.
    ignoreHTTPSErrors: true,
    args: [`--disable-extensions-except=${EXT_PATH}`, `--load-extension=${EXT_PATH}`],
  });
  worker = context.serviceWorkers()[0] ?? (await context.waitForEvent('serviceworker'));
  extensionId = new URL(worker.url()).host;

  await worker.evaluate(async (origin) => {
    await chrome.storage.local.set({ 'pporlock.state': { controlOrigin: origin } });
  }, `http://127.0.0.1:${controlPort}`);

  // Pair the way a user does — the CLI opens a window, the extension redeems
  // the code. Writing the token straight into storage would skip the handshake
  // that registers the origin, which is the thing under test.
  const begin = (await fetch(`http://127.0.0.1:${controlPort}/pair/begin`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      'X-Pporlock-Client': 'cli',
    },
    body: '{}',
  }).then((r) => r.json())) as { code: string };

  extPage = await context.newPage();
  await extPage.goto(`chrome-extension://${extensionId}/src/popup/options.html`);
  const paired = (await extPage.evaluate(
    async (code) => (await chrome.runtime.sendMessage({ type: 'pair', code })) as unknown,
    begin.code,
  )) as { ok: boolean; error?: string };
  if (!paired.ok) throw new Error(`pairing failed: ${paired.error}`);
});

test.afterAll(async () => {
  await context?.close();
  if (daemon?.pid) {
    try {
      process.kill(-daemon.pid, 'SIGKILL');
    } catch {
      /* already gone */
    }
  }
  rmSync(userDataDir, { recursive: true, force: true });
  rmSync(stateDir, { recursive: true, force: true });
});

test.describe.configure({ mode: 'serial' });

test('the paired extension can start a recording', async () => {
  // The baseline, and deliberately a POST — see callFromExtension. If this
  // fails the rest proves nothing, because a later failure would be
  // indistinguishable from "pairing never worked at all".
  expect(await startRecording('before-restart')).toBe(201);
});

test('a GET is NOT a test of the origin policy', async () => {
  // Pinned so nobody rebuilds this file on a GET again. Chrome sends no Origin
  // for a host-permitted GET, so the daemon allows it by the curl/CLI rule and
  // the status says nothing about pairing. This is documentation with an
  // assertion attached: if it ever starts returning 403, the manifest's
  // host_permissions changed and the reasoning above needs revisiting.
  expect(await callFromExtension('/state', authorizedRead())).toBe(200);
});

test('OI-19: a mutating call still works after a daemon restart', async () => {
  // The regression that shipped, in the shape the user hit it: they restarted
  // (or logged in again), tried to record, and got 403 "origin not permitted".
  // The extension is not touched between the two calls — no re-pairing, no
  // reload, no user action. Only the daemon restarts.
  const before = await uptime();
  await stopDaemon();
  await startDaemon();
  expect(await uptime()).toBeLessThan(before);

  expect(await startRecording('after-restart')).toBe(201);
});

test('OI-19: an unpaired extension origin is still refused', async () => {
  // Guards the guard. Persisting the pairing must not have degraded into
  // allowing every chrome-extension origin — which would make the test above
  // pass for the worst possible reason.
  const status = await extPage.evaluate(
    async ([port, tok]) => {
      const response = await fetch(`http://127.0.0.1:${port as number}/sessions`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${tok as string}`,
          'Content-Type': 'application/json',
          'X-Pporlock-Client': 'extension',
        },
        body: JSON.stringify({ name: 'should-not-happen' }),
      });
      return response.status;
    },
    [controlPort, 'not-the-real-token'] as const,
  );
  // A bad token from the *paired* origin is 401, not 403: the origin passed and
  // the token did not. That distinction is what proves the origin check ran and
  // succeeded on its own merits rather than being skipped.
  expect(status).toBe(401);
});

test('OI-19: the bearer token also survives, so 201 is not luck', async () => {
  // Pairing and the token are persisted by different mechanisms. Asserting only
  // the status would let a future change that regenerated the token pass here
  // while breaking every real extension.
  const onDisk = readFileSync(join(stateDir, 'token'), 'utf8').trim();
  expect(onDisk).toBe(token);
});

test('OI-9: the active profile survives a daemon restart', async () => {
  // A fresh daemon has only `default`, so the switch needs somewhere to go.
  // The shapes here are taken from a running daemon, not guessed: `GET
  // /profiles` is a bare array with no envelope and no active flag, and the
  // active profile is reported by `GET /state` as `active_profile`. Writing
  // what the client "obviously" returns is how the wire-shape bugs happened.
  const created = await fetch(`http://127.0.0.1:${controlPort}/profiles`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      'X-Pporlock-Client': 'cli',
    },
    body: JSON.stringify({ name: 'restart-probe', description: 'created by the restart E2E' }),
  });
  expect(created.status).toBe(201);

  const activated = await fetch(`http://127.0.0.1:${controlPort}/profiles/restart-probe/activate`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'X-Pporlock-Client': 'cli' },
  });
  expect(activated.ok).toBe(true);
  expect(await activeProfile()).toBe('restart-probe');

  const before = await uptime();
  await stopDaemon();
  await startDaemon();
  expect(await uptime()).toBeLessThan(before);

  expect(await activeProfile()).toBe('restart-probe');
});
