/**
 * The fail-safe. REQ EXT-010, PXY-008.
 *
 * This suite gates the sprint. It prevents the worst failure the system can
 * produce: the daemon dies and the browser is left pointed at a proxy that is
 * no longer there, so every page load fails with nothing to explain why.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlApi } from '../shared/api';
import { StateStore } from '../shared/state';
import { FAILURE_THRESHOLD, HealthMonitor } from './health';
import { ProxyController } from './proxy';
import { FakeProxyApi, FakeStorage } from '../test/fakes';

const ORIGIN = 'http://127.0.0.1:8081';

interface Harness {
  monitor: HealthMonitor;
  proxyApi: FakeProxyApi;
  store: StateStore;
  api: ControlApi;
  trips: string[];
  recoveries: number;
}

function harness(): Harness {
  const local = new FakeStorage();
  const session = new FakeStorage();
  const store = new StateStore(local, session);
  const proxyApi = new FakeProxyApi();
  const proxy = new ProxyController(proxyApi);
  const api = new ControlApi(ORIGIN);
  const trips: string[] = [];
  let recoveries = 0;

  const monitor = new HealthMonitor({
    api,
    proxy,
    store,
    onTrip: (reason) => {
      trips.push(reason);
    },
    onRecover: () => {
      recoveries += 1;
    },
  });

  return {
    monitor,
    proxyApi,
    store,
    api,
    trips,
    get recoveries() {
      return recoveries;
    },
  } as Harness;
}

function healthy(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true, version: '0.1.0' }) }),
  );
}

function dead(): void {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe('HealthMonitor', () => {
  it('does nothing while the proxy is off — there is nothing to protect', async () => {
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: false });

    expect(await h.monitor.check()).toBe(true);
    expect(h.monitor.consecutiveFailures).toBe(0);
    expect(h.proxyApi.clearCalls).toBe(0);
  });

  it('reports healthy when the daemon answers', async () => {
    healthy();
    const h = harness();
    await h.store.save({ proxyEnabled: true });

    expect(await h.monitor.check()).toBe(true);
    expect(h.monitor.consecutiveFailures).toBe(0);
  });

  it('does not trip on a single failure', async () => {
    // A dropped request during a daemon restart must not tear down a working
    // setup — hence a threshold of two, not one.
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });

    await h.monitor.check();
    expect(h.monitor.consecutiveFailures).toBe(1);
    expect(h.proxyApi.clearCalls).toBe(0);
    expect((await h.store.load()).proxyEnabled).toBe(true);
  });

  it('CLEARS THE PROXY on the second consecutive failure', async () => {
    // The load-bearing assertion of the entire extension.
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true, proxyApplied: true });

    for (let i = 0; i < FAILURE_THRESHOLD; i += 1) await h.monitor.check();

    expect(h.proxyApi.clearCalls).toBeGreaterThan(0);
    expect(h.proxyApi.config).toBeNull();
  });

  it('records that it tripped, so the popup can explain rather than show "off"', async () => {
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });

    for (let i = 0; i < FAILURE_THRESHOLD; i += 1) await h.monitor.check();

    const state = await h.store.load();
    expect(state.proxyEnabled).toBe(false);
    expect(state.proxyApplied).toBe(false);
    expect(state.failSafeTrippedAt).not.toBeNull();
    expect(state.lastError?.code).toBe('daemon_unreachable');
  });

  it('tells the user their browsing is working again', async () => {
    // The message has to answer the question the user actually has.
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });
    for (let i = 0; i < FAILURE_THRESHOLD; i += 1) await h.monitor.check();

    const message = (await h.store.load()).lastError?.message ?? '';
    expect(message).toMatch(/working again/i);
    expect(message).toMatch(/pporlock run/);
  });

  it('notifies the caller when it trips', async () => {
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });
    for (let i = 0; i < FAILURE_THRESHOLD; i += 1) await h.monitor.check();
    expect(h.trips).toContain('daemon_unreachable');
  });

  it('does NOT auto-re-enable when the daemon returns', async () => {
    // A daemon that crashed once may crash again mid-page-load. Re-enabling is
    // a deliberate user action (SPEC-3 §4.4 rule 4).
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });
    for (let i = 0; i < FAILURE_THRESHOLD; i += 1) await h.monitor.check();

    healthy();
    await h.monitor.check();

    const state = await h.store.load();
    expect(state.proxyEnabled).toBe(false);
    expect(h.proxyApi.config).toBeNull();
  });

  it('trips only once, not on every subsequent check', async () => {
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });
    for (let i = 0; i < 6; i += 1) await h.monitor.check();
    expect(h.trips).toHaveLength(1);
  });

  it('resets the failure count after a recovery', async () => {
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });
    await h.monitor.check();
    expect(h.monitor.consecutiveFailures).toBe(1);

    healthy();
    await h.monitor.check();
    expect(h.monitor.consecutiveFailures).toBe(0);
  });

  it('treats a non-ok health body as a failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: false }) }),
    );
    const h = harness();
    await h.store.save({ proxyEnabled: true });
    await h.monitor.check();
    expect(h.monitor.consecutiveFailures).toBe(1);
  });

  it('treats an HTTP error as a failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500 }));
    const h = harness();
    await h.store.save({ proxyEnabled: true });
    await h.monitor.check();
    expect(h.monitor.consecutiveFailures).toBe(1);
  });

  it('treats a hung daemon as a failure rather than waiting forever', async () => {
    // A daemon that accepts the connection and never answers is exactly as
    // broken as one that is gone, and must not stall the check.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () => reject(new Error('aborted')));
          }),
      ),
    );
    const h = harness();
    await h.store.save({ proxyEnabled: true });

    const result = await h.monitor.check();
    expect(result).toBe(false);
  }, 10_000);

  it('still records the trip when clearing the proxy itself fails', async () => {
    // If even clearing fails there is nothing more we can do, but the user must
    // still be told where to look.
    dead();
    const h = harness();
    h.proxyApi.failOnClear = true;
    await h.store.save({ proxyEnabled: true });

    for (let i = 0; i < FAILURE_THRESHOLD; i += 1) await h.monitor.check();

    expect((await h.store.load()).failSafeTrippedAt).not.toBeNull();
    expect(h.trips).toHaveLength(1);
  });

  it('reset() clears state for a deliberate re-enable', async () => {
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });
    await h.monitor.check();
    h.monitor.reset();
    expect(h.monitor.consecutiveFailures).toBe(0);
    expect(h.monitor.healthy).toBeNull();
  });

  it('start and stop are idempotent', () => {
    const h = harness();
    h.monitor.start(50);
    h.monitor.start(50);
    h.monitor.stop();
    h.monitor.stop();
  });
});
