/**
 * The fail-safe (SPEC-3 §4.4, REQ EXT-010, PXY-008).
 *
 * This prevents the worst failure the system can produce: the daemon dies, and
 * the browser is left pointed at a proxy that is no longer there. Every page
 * load then fails, and nothing tells the user why.
 *
 * The rules, restated here because each one exists for a reason:
 *
 *  1. While the proxy is enabled, poll /state/health. Driven by chrome.alarms
 *     as well as a timer, because an MV3 worker is suspended aggressively and a
 *     suspended worker cannot notice anything.
 *  2. On two consecutive failures, clear Chrome's proxy configuration. Two, not
 *     one: a single dropped request during a daemon restart should not tear
 *     down a working setup.
 *  3. Set an unmistakable error state. The user must learn that pporlock turned
 *     itself off, not discover it by finding the internet broken.
 *  4. Do NOT auto-re-enable when the daemon returns. A daemon that crashed once
 *     may crash again mid-page-load; re-enabling is a deliberate user action.
 *  5. The check must never route through the proxy — see bypassList().
 *  6. chrome.runtime.onSuspend is best-effort and is not relied upon.
 */
import type { ControlApi } from '../shared/api';
import type { ProxyController } from './proxy';
import type { StateStore } from '../shared/state';

export const POLL_INTERVAL_MS = 10_000;
export const FAILURE_THRESHOLD = 2;
export const HEALTH_TIMEOUT_MS = 3_000;

export interface HealthDeps {
  api: ControlApi;
  proxy: ProxyController;
  store: StateStore;
  /** Called when the fail-safe trips, so the badge and notification can react. */
  onTrip?: (reason: string) => void | Promise<void>;
  onRecover?: () => void | Promise<void>;
}

export class HealthMonitor {
  private failures = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastOk: boolean | null = null;

  constructor(private readonly deps: HealthDeps) {}

  get consecutiveFailures(): number {
    return this.failures;
  }

  get healthy(): boolean | null {
    return this.lastOk;
  }

  start(intervalMs: number = POLL_INTERVAL_MS): void {
    this.stop();
    this.timer = setInterval(() => void this.check(), intervalMs);
  }

  stop(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
  }

  /** Reset after a deliberate user re-enable. */
  reset(): void {
    this.failures = 0;
    this.lastOk = null;
  }

  /**
   * One health check. Returns whether the daemon answered.
   *
   * Safe to call from an alarm, a timer, or the popup opening — all three are
   * wanted, and none may throw into its caller.
   */
  async check(): Promise<boolean> {
    const state = await this.deps.store.load();

    // Nothing to protect if we are not holding the proxy.
    if (!state.proxyEnabled) {
      this.failures = 0;
      return true;
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);
    let ok = false;
    try {
      const health = await this.deps.api.health(controller.signal);
      ok = health.ok === true;
    } catch {
      ok = false;
    } finally {
      clearTimeout(timeout);
    }

    this.lastOk = ok;

    if (ok) {
      const recovered = this.failures > 0;
      this.failures = 0;
      if (recovered) await this.deps.onRecover?.();
      return true;
    }

    this.failures += 1;
    if (this.failures >= FAILURE_THRESHOLD) {
      await this.trip();
    }
    return false;
  }

  /**
   * Clear Chrome's proxy configuration and record why.
   *
   * proxyEnabled goes false so nothing re-applies it on the next worker wake,
   * and failSafeTrippedAt is recorded so the popup can say what happened rather
   * than merely showing "off".
   */
  private async trip(): Promise<void> {
    const state = await this.deps.store.load();
    if (!state.proxyEnabled && state.failSafeTrippedAt !== null) return;

    try {
      await this.deps.proxy.disable();
    } catch {
      /* If even clearing fails there is nothing further we can do from here,
         but the error state below still tells the user where to look. */
    }

    await this.deps.store.save({
      proxyEnabled: false,
      proxyApplied: false,
      failSafeTrippedAt: Date.now(),
      lastError: {
        code: 'daemon_unreachable',
        message:
          'pporlock turned the proxy off because the daemon stopped responding. ' +
          'Your browsing is working again. Start it with `pporlock run`, then turn ' +
          'the proxy back on.',
        at: Date.now(),
      },
    });

    await this.deps.onTrip?.('daemon_unreachable');
  }
}
