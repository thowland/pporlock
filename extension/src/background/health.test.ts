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
import {
  HEALTH_TIMEOUT_MAX_MS,
  HEALTH_TIMEOUT_MS,
  HealthMonitor,
  REFUSED_THRESHOLD,
  UNRESPONSIVE_THRESHOLD,
  classifyFailure,
} from './health';
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

    for (let i = 0; i < REFUSED_THRESHOLD; i += 1) await h.monitor.check();

    expect(h.proxyApi.clearCalls).toBeGreaterThan(0);
    expect(h.proxyApi.config).toBeNull();
  });

  it('records that it tripped, so the popup can explain rather than show "off"', async () => {
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });

    for (let i = 0; i < REFUSED_THRESHOLD; i += 1) await h.monitor.check();

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
    for (let i = 0; i < REFUSED_THRESHOLD; i += 1) await h.monitor.check();

    const message = (await h.store.load()).lastError?.message ?? '';
    expect(message).toMatch(/working again/i);
    expect(message).toMatch(/pporlock run/);
  });

  it('notifies the caller when it trips', async () => {
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });
    for (let i = 0; i < REFUSED_THRESHOLD; i += 1) await h.monitor.check();
    expect(h.trips).toContain('daemon_unreachable');
  });

  it('does NOT auto-re-enable when the daemon returns', async () => {
    // A daemon that crashed once may crash again mid-page-load. Re-enabling is
    // a deliberate user action (SPEC-3 §4.4 rule 4).
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });
    for (let i = 0; i < REFUSED_THRESHOLD; i += 1) await h.monitor.check();

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

    for (let i = 0; i < REFUSED_THRESHOLD; i += 1) await h.monitor.check();

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

/**
 * A daemon that is alive and too busy to answer — OI-22.
 *
 * `slow()` aborts the way our own timeout does, which is what the extension
 * sees when the daemon is saturated rather than gone. Before this distinction
 * existed, that was indistinguishable from a dead daemon and disabled a
 * perfectly working system. It was diagnosed by the user re-enabling the
 * extension without restarting the daemon: proof the daemon had been alive the
 * whole time.
 */
function slow(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(
      (_url: string, init?: { signal?: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          const signal = init?.signal;
          if (signal?.aborted) {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
            return;
          }
          signal?.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          );
        }),
    ),
  );
}

describe('classifyFailure', () => {
  it('reads an aborted fetch as a timeout', () => {
    expect(classifyFailure(new DOMException('aborted', 'AbortError'))).toBe('timeout');
  });

  it('reads a transport failure to loopback as refused', () => {
    // Nothing is listening on a port on this machine.
    expect(classifyFailure(new TypeError('Failed to fetch'))).toBe('refused');
  });

  it('defaults an unrecognised error to refused, the stricter path', () => {
    // The safe direction: an unexpected shape gets the pre-existing behaviour,
    // so this change can never make the fail-safe trip later than it used to
    // for a genuinely dead daemon.
    expect(classifyFailure({ weird: true })).toBe('refused');
  });
});

describe('HealthMonitor — slow is not dead (OI-22)', () => {
  it('does NOT trip at the refused threshold when checks merely time out', async () => {
    // The regression that disabled a working system. Two timeouts used to be
    // a trip; a busy daemon now gets more rope than a missing one.
    vi.useFakeTimers();
    try {
      slow();
      const h = harness();
      await h.store.save({ proxyEnabled: true });

      for (let i = 0; i < REFUSED_THRESHOLD; i += 1) {
        const check = h.monitor.check();
        await vi.advanceTimersByTimeAsync(HEALTH_TIMEOUT_MAX_MS + 1_000);
        await check;
      }

      expect(h.trips).toEqual([]);
      expect(h.proxyApi.clearCalls).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('still trips eventually, so a wedged daemon is not tolerated forever', async () => {
    // The fail-safe must remain a fail-safe. More patient, not absent.
    vi.useFakeTimers();
    try {
      slow();
      const h = harness();
      await h.store.save({ proxyEnabled: true });

      for (let i = 0; i < UNRESPONSIVE_THRESHOLD; i += 1) {
        const check = h.monitor.check();
        await vi.advanceTimersByTimeAsync(HEALTH_TIMEOUT_MAX_MS + 1_000);
        await check;
      }

      expect(h.trips).toEqual(['daemon_unresponsive']);
      expect(h.proxyApi.clearCalls).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not tell the user to start a daemon that is already running', async () => {
    // OI-18's lesson applied here: a confident wrong instruction costs more
    // than none, because it gets followed.
    vi.useFakeTimers();
    try {
      slow();
      const h = harness();
      await h.store.save({ proxyEnabled: true });
      for (let i = 0; i < UNRESPONSIVE_THRESHOLD; i += 1) {
        const check = h.monitor.check();
        await vi.advanceTimersByTimeAsync(HEALTH_TIMEOUT_MAX_MS + 1_000);
        await check;
      }

      const state = await h.store.load();
      expect(state.lastError?.code).toBe('daemon_unresponsive');
      expect(state.lastError?.message).not.toContain('pporlock run');
      expect(state.lastError?.message).toContain('overloaded');
    } finally {
      vi.useRealTimers();
    }
  });

  it('a refused daemon still trips at two, unchanged', async () => {
    // The guard on the guard. Making the busy case patient must not have made
    // the dead case slower — that would be a regression in the thing the
    // fail-safe exists for.
    dead();
    const h = harness();
    await h.store.save({ proxyEnabled: true });

    for (let i = 0; i < REFUSED_THRESHOLD; i += 1) await h.monitor.check();

    expect(h.trips).toEqual(['daemon_unreachable']);
  });

  it('escalates the timeout budget so a later check is more patient', async () => {
    vi.useFakeTimers();
    try {
      slow();
      const h = harness();
      await h.store.save({ proxyEnabled: true });

      expect(h.monitor.timeoutBudgetMs).toBe(HEALTH_TIMEOUT_MS);

      const first = h.monitor.check();
      await vi.advanceTimersByTimeAsync(HEALTH_TIMEOUT_MAX_MS + 1_000);
      await first;

      expect(h.monitor.timeoutBudgetMs).toBe(HEALTH_TIMEOUT_MS * 2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('one good answer clears the slate', async () => {
    // Recovery matters more here than before: a transient stall under load is
    // now the expected case, not an anomaly.
    vi.useFakeTimers();
    try {
      slow();
      const h = harness();
      await h.store.save({ proxyEnabled: true });
      const check = h.monitor.check();
      await vi.advanceTimersByTimeAsync(HEALTH_TIMEOUT_MAX_MS + 1_000);
      await check;
      expect(h.monitor.consecutiveFailures).toBe(1);

      vi.useRealTimers();
      healthy();
      expect(await h.monitor.check()).toBe(true);
      expect(h.monitor.consecutiveFailures).toBe(0);
      expect(h.monitor.timeoutBudgetMs).toBe(HEALTH_TIMEOUT_MS);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a mixture of timeouts and refusals still terminates', async () => {
    // Without the total-failure ceiling, alternating causes could reset each
    // other's counter forever and the fail-safe would never fire.
    const h = harness();
    await h.store.save({ proxyEnabled: true });

    for (let i = 0; i < UNRESPONSIVE_THRESHOLD; i += 1) {
      if (i % 2 === 0) {
        vi.useRealTimers();
        dead();
        await h.monitor.check();
      } else {
        vi.useFakeTimers();
        slow();
        const check = h.monitor.check();
        await vi.advanceTimersByTimeAsync(HEALTH_TIMEOUT_MAX_MS + 1_000);
        await check;
        vi.useRealTimers();
      }
      if (h.trips.length > 0) break;
    }

    expect(h.trips.length).toBe(1);
  });
});
