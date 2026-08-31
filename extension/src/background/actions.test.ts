/**
 * The worker's daemon-facing actions. REQ EXT-024, OI-34.
 *
 * These tests exist because the fix for OI-34 was reported as not working, and
 * nothing in the suite could say whether it did: the code lived in `index.ts`,
 * which registers chrome listeners at module load and is excluded from
 * coverage. "I read the code and it looks right" is what a test is for.
 *
 * They drive the real functions against a real ControlApi with `fetch` stubbed
 * to the daemon's actual 403 — status, `error.code` and `error.message` copied
 * from a live `GET /state/health` on an unpaired install, not invented.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlApi } from '../shared/api';
import { StateStore } from '../shared/state';
import { DAEMON_UNREACHABLE_MESSAGE } from '../shared/errors';
import { HealthMonitor, POLL_INTERVAL_MS } from './health';
import { ProxyController } from './proxy';
import { FakeProxyApi, FakeStorage } from '../test/fakes';
import { type ActionDeps, disableProxy, enableProxy, status } from './actions';

const ORIGIN = 'http://127.0.0.1:8081';

function harness(): { deps: ActionDeps; store: StateStore; proxyApi: FakeProxyApi } {
  const store = new StateStore(new FakeStorage(), new FakeStorage());
  const proxyApi = new FakeProxyApi();
  const proxy = new ProxyController(proxyApi);
  const api = new ControlApi(ORIGIN);
  const health = new HealthMonitor({ api, proxy, store });

  return {
    store,
    proxyApi,
    deps: {
      client: async () => api,
      store,
      proxy,
      health,
      refreshBadge: async () => {},
      attributionGranted: async () => false,
      extensionVersion: () => '0.10.0',
      pollIntervalMs: POLL_INTERVAL_MS,
    },
  };
}

/** What an unpaired extension gets from every route, /state/health included. */
function refusing(status = 403): void {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: false,
      status,
      statusText: 'Forbidden',
      json: async () => ({
        error: {
          code: 'unauthorized',
          message: 'origin not permitted',
          detail: { origin: 'chrome-extension://' + 'a'.repeat(32) },
        },
      }),
    }),
  );
}

const DAEMON_STATE = {
  version: '0.10.0',
  proxy: { running: true, listen: '127.0.0.1:8080', uptime_s: 1 },
  active_profile: 'default',
  dev_toggles: { anticache: false, anticomp: false },
  modules: { loaded: 10, enabled: 1, quarantined: 0, errors: [] },
  capture: { ring_flows: 0, ring_bytes: 0, recording_session: 'sess-1' },
  counters: { flows_total: 7, blocked: 1, modified: 2, passthrough: 4, errors: 0 },
};

/** Routes by path, because status() calls two different endpoints. */
function paired(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () =>
        String(url).endsWith('/profiles')
          ? [{ name: 'default' }, { name: 'strict' }]
          : DAEMON_STATE,
    })),
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe('enableProxy against an unpaired daemon', () => {
  it('fails, rather than turning the proxy on', async () => {
    // The whole complaint: the pairing error must arrive here, at the toggle,
    // not three actions later when the user tries to record a session.
    refusing();
    const h = harness();

    const reply = await enableProxy(h.deps);

    expect(reply.ok).toBe(false);
    expect(h.proxyApi.setCalls).toBe(0);
    expect((await h.store.load()).proxyApplied).toBe(false);
  });

  it('names pairing as the remedy, not restarting the daemon', async () => {
    // The misdiagnosis: a blind catch reported every failure as unreachable,
    // sending the user to restart the one component that was working.
    refusing();
    const h = harness();

    const reply = await enableProxy(h.deps);

    expect(reply.error).toContain('pporlock pair');
    expect(reply.error).not.toBe(DAEMON_UNREACHABLE_MESSAGE);
    expect(reply.error).not.toContain('origin not permitted');
  });

  it('drops the pairing claim, so the popup offers the prompt that fixes it', async () => {
    refusing();
    const h = harness();
    await h.store.save({ paired: true });

    await enableProxy(h.deps);

    const state = await h.store.load();
    expect(state.paired).toBe(false);
    expect(state.lastError?.code).toBe('unpaired');
  });

  it('still says to start the daemon when it really is not there', async () => {
    // The narrowness matters in both directions: a transport failure keeps the
    // message it always had.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const h = harness();

    const reply = await enableProxy(h.deps);

    expect(reply.ok).toBe(false);
    expect(reply.error).toBe(DAEMON_UNREACHABLE_MESSAGE);
  });

  it('turns the proxy on when the daemon is paired — the guard is not just "always fail"', async () => {
    paired();
    const h = harness();

    const reply = await enableProxy(h.deps);

    expect(reply.ok).toBe(true);
    expect((await h.store.load()).proxyApplied).toBe(true);
  });
});

describe('status against an unpaired daemon', () => {
  it('reports the daemon as reachable, because it answered', async () => {
    // The popup renders its pairing prompt only when the daemon is reachable.
    // Scoring a 403 as "down" hid the one control that fixes a 403.
    refusing();
    const h = harness();

    const reply = await status(h.deps);

    expect(reply.daemonReachable).toBe(true);
    expect(reply.state.paired).toBe(false);
    expect(reply.state.lastError?.code).toBe('unpaired');
  });

  it('reports it unreachable when nothing answers at all', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const h = harness();

    expect((await status(h.deps)).daemonReachable).toBe(false);
  });
});

describe('status against a paired daemon', () => {
  it('reports the counters, the version and the profiles', async () => {
    // The other half of the guard: these tests must be able to tell a working
    // daemon from a refusing one, or "reachable" proves nothing.
    paired();
    const h = harness();
    await h.store.save({ paired: true });

    const reply = await status(h.deps);

    expect(reply.daemonReachable).toBe(true);
    expect(reply.version).toBe('0.10.0');
    expect(reply.counters).toEqual({ flows: 7, blocked: 1, modified: 2, passthrough: 4 });
    expect(reply.profiles).toEqual(['default', 'strict']);
  });

  it('mirrors the daemon\u2019s state so the popup never shows a stale toggle', async () => {
    paired();
    const h = harness();
    await h.store.save({ paired: true });

    await status(h.deps);

    const state = await h.store.load();
    expect(state.activeProfile).toBe('default');
    expect(state.recordingSession).toBe('sess-1');
    expect(state.moduleHealth).toEqual({ errors: 0, quarantined: 0 });
  });

  it('does not ask for profiles it has no pairing to fetch them with', async () => {
    paired();
    const h = harness();
    await h.store.save({ paired: false });

    expect((await status(h.deps)).profiles).toEqual([]);
  });
});

describe('disableProxy', () => {
  it('clears the proxy without asking the daemon anything', async () => {
    // Turning interception off must work when the daemon is gone, refusing, or
    // on fire. It is the fail-safe's manual equivalent.
    refusing();
    const h = harness();
    await h.store.save({ proxyEnabled: true, proxyApplied: true });

    const reply = await disableProxy(h.deps);

    expect(reply.ok).toBe(true);
    expect(h.proxyApi.clearCalls).toBe(1);
    expect((await h.store.load()).proxyApplied).toBe(false);
  });
});
