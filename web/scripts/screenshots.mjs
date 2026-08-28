/**
 * Capture the README screenshots against a real system.
 *
 * Lives under web/ so node resolves @playwright/test from web/node_modules;
 * it is a repository-level task rather than a web one.
 *
 * Deliberately not mocks. A screenshot of a fixture is a drawing of what we
 * hoped the tool looks like; these run a real daemon, push real traffic through
 * the real proxy, load a real module, and photograph what came out. If the UI
 * is broken, the README shows it broken, which is the point.
 *
 *   node scripts/screenshots.mjs [--out docs/images] [--keep]
 */
import { chromium } from '@playwright/test';
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { networkInterfaces, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const argv = process.argv.slice(2);
const OUT = resolve(REPO, argv.includes('--out') ? argv[argv.indexOf('--out') + 1] : 'docs/images');
const KEEP = argv.includes('--keep');

const log = (...a) => console.log('[shots]', ...a);

function freePort() {
  return new Promise((res, rej) => {
    const s = createServer();
    s.unref();
    s.on('error', rej);
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => res(port));
    });
  });
}

/**
 * The fixture must not be on loopback: the proxy bypass list excludes it, so a
 * loopback fixture is never proxied and the flow table would be empty. Same
 * constraint the E2E suite hit.
 */
function lanAddress() {
  for (const [name, addrs] of Object.entries(networkInterfaces())) {
    if (name.startsWith('lo')) continue;
    for (const a of addrs ?? []) if (a.family === 'IPv4' && !a.internal) return a.address;
  }
  return null;
}

const children = [];
function spawnDetached(cmd, args, opts) {
  const child = spawn(cmd, args, { detached: true, stdio: 'ignore', ...opts });
  children.push(child);
  return child;
}

function killAll() {
  for (const c of children) {
    if (c.pid) {
      try {
        process.kill(-c.pid, 'SIGKILL');
      } catch {
        /* already gone */
      }
    }
  }
}

async function waitFor(url, tries = 120, ms = 500) {
  for (let i = 0; i < tries; i += 1) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, ms));
  }
  throw new Error(`never came up: ${url}`);
}

/** A module worth photographing: it does something visible and says so. */
function writeModule(stateDir) {
  const dir = join(stateDir, 'modules', 'relax-csp');
  mkdirSync(join(dir, 'assets'), { recursive: true });
  writeFileSync(
    join(dir, 'module.yaml'),
    [
      'name: relax-csp',
      "pporlock_api: '1'",
      'description: Relaxes CSP and strips SRI on the staging host, so a local bundle can load.',
      'author: pporlock',
      'enabled: true',
      'priority: 100',
      'rules:',
      '  - name: strip-csp',
      '    action: headers',
      "    match: {host: '*'}",
      '    response:',
      '      remove: [content-security-policy, content-security-policy-report-only]',
      '  - name: drop-sri',
      '    action: body',
      "    match: {host: '*', content_type: 'text/html'}",
      '    transform: {kind: strip_integrity_attributes}',
      '  - name: block-the-tracker',
      '    action: block',
      "    match: {path: '^/dest/script'}",
      '    mode: stub',
      '',
    ].join('\n'),
  );
}

async function seedTraffic(proxyPort, base) {
  // Through the proxy, so these become real flows with real provenance.
  const paths = [
    '/csp/nonce',
    '/csp/nonce',
    '/dest/script',
    '/health',
    '/encoded?enc=gzip',
    '/nope',
    '/dest/style',
    '/dest/image',
  ];
  for (const p of paths) {
    await new Promise((res) => {
      const c = spawn('curl', ['-s', '-o', '/dev/null', '-x', `http://127.0.0.1:${proxyPort}`, `${base}${p}`]);
      c.on('exit', res);
    });
  }
}

async function main() {
  const host = lanAddress();
  if (host === null) throw new Error('no non-loopback interface; the fixture would be bypassed');

  mkdirSync(OUT, { recursive: true });
  const stateDir = mkdtempSync(join(tmpdir(), 'pporlock-shots-state-'));
  const userDataDir = mkdtempSync(join(tmpdir(), 'pporlock-shots-chrome-'));
  const [proxyPort, controlPort, fixturePort] = [await freePort(), await freePort(), await freePort()];

  writeModule(stateDir);
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

  log('starting fixture origin');
  spawnDetached('uv', ['run', 'python', '../testfixtures/origin/server.py', '--host', host, '--port', String(fixturePort)], { cwd: join(REPO, 'daemon') });
  await waitFor(`http://${host}:${fixturePort}/health`);

  log('starting daemon');
  spawnDetached('uv', ['run', 'pporlock', '--config', join(stateDir, 'config.yaml'), 'run'], { cwd: join(REPO, 'daemon') });
  await waitFor(`http://127.0.0.1:${controlPort}/state/health`);

  const control = `http://127.0.0.1:${controlPort}`;
  const token = readFileSync(join(stateDir, 'token'), 'utf8').trim();
  const auth = {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
    'X-Pporlock-Client': 'ui',
  };

  // A recorded session, so the sessions view is not an empty state. Recording
  // real traffic rather than inserting rows: a screenshot of a fixture is a
  // drawing of what we hoped the tool looks like.
  log('recording a session');
  const started = await (
    await fetch(`${control}/sessions`, {
      method: 'POST',
      headers: auth,
      body: JSON.stringify({ name: 'checkout-bug' }),
    })
  ).json();
  await seedTraffic(proxyPort, `http://${host}:${fixturePort}`);
  await new Promise((r) => setTimeout(r, 1200));
  await fetch(`${control}/sessions/${started.session_id}/stop`, { method: 'POST', headers: auth });

  log('seeding live traffic');
  await seedTraffic(proxyPort, `http://${host}:${fixturePort}`);
  await new Promise((r) => setTimeout(r, 1500));

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2, // Retina, so the README is legible.
    colorScheme: 'dark',
  });

  // The UI needs the bearer token. It is normally obtained by pairing; here we
  // read it the way the CLI does, because this script runs as the user.
  // sessionStorage, which is where main.tsx keeps it after reading it from the
  // fragment — a token in the address bar ends up in browser history.
  await context.addInitScript((t) => {
    // Guarded: this runs on every document including about:blank, where
    // sessionStorage access throws outright.
    try {
      window.sessionStorage.setItem('pporlock.token', t);
    } catch {
      /* opaque origin */
    }
  }, token);

  const page = await context.newPage();
  // A blank screenshot is indistinguishable from a working one at a glance, so
  // page errors are surfaced rather than left to be noticed later.
  page.on('pageerror', (e) => log('PAGE ERROR:', e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') log('CONSOLE ERROR:', m.text());
  });
  const origin = control;

  // Provenance first, while the traffic view is freshly mounted — doing it last
  // meant arriving by a hash change from another view, and the table had not
  // refetched. It is the most important screen in the application, so it gets
  // its own shot with a flow actually selected.
  log('capturing provenance — the flow detail panel');
  await page.goto(`${origin}/#/`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  // Pick the flow that had the most done to it — the blocked one — because a
  // provenance screenshot of a flow nothing touched shows nothing.
  const row = page.locator('tbody tr').filter({ hasText: '/csp/nonce' }).first();
  await row.waitFor({ state: 'visible', timeout: 20_000 });
  await row.click();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: join(OUT, 'provenance.png') });

  log('capturing blocked — a short-circuited flow');
  const blocked = page.locator('tbody tr').filter({ hasText: '/dest/script' }).first();
  await blocked.click();
  await page.waitForTimeout(1200);
  await page.screenshot({ path: join(OUT, 'provenance-blocked.png') });


  const shots = [
    ['traffic', '/#/', 'the live flow table'],
    ['modules', '/#/modules', 'the module library'],
    ['module-editor', `/#/modules/relax-csp`, 'the module editor'],
    ['sessions', '/#/sessions', 'recorded sessions'],
    ['settings', '/#/settings', 'redaction settings'],
    ['profiles', '/#/profiles', 'profiles'],
  ];

  for (const [name, path, what] of shots) {
    log(`capturing ${name} — ${what}`);
    await page.goto('about:blank');
    await page.goto(`${origin}${path}`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2200);
    await page.screenshot({ path: join(OUT, `${name}.png`) });
  }

  await browser.close();
  log(`wrote screenshots to ${OUT}`);

  if (!KEEP) {
    rmSync(stateDir, { recursive: true, force: true });
    rmSync(userDataDir, { recursive: true, force: true });
  } else {
    log(`kept state at ${stateDir}`);
  }
}

try {
  await main();
} finally {
  killAll();
}
