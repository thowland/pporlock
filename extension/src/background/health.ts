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
 *  2. On repeated failures, clear Chrome's proxy configuration — but how many
 *     it takes depends on *how* the check failed, because "refused" and "timed
 *     out" are different facts (OI-22).
 *
 *     A refused connection is definitive: nothing is listening, the daemon is
 *     gone, and two strikes is right. A timeout is not — it means the daemon
 *     did not answer within the budget, and a saturated daemon is still a live
 *     one. Under load the health endpoint measured 3.8 ms idle against 37.7 ms
 *     at 32 concurrent requests: a tenfold degradation, still well inside the
 *     budget, but the direction of travel is real and HTTPS is heavier than
 *     anything that was measured. Treating slow as dead disables a working
 *     system, and the user then re-enables it without restarting anything —
 *     which is exactly how this was diagnosed.
 *  3. Set an unmistakable error state, and say which of the two happened. A
 *     message telling someone to run `pporlock run` when the daemon is already
 *     running sends them to re-run the one thing that is not the problem.
 *  4. Do NOT auto-re-enable when the daemon returns. A daemon that crashed once
 *     may crash again mid-page-load; re-enabling is a deliberate user action.
 *  5. The check must never route through the proxy — see bypassList().
 *  6. chrome.runtime.onSuspend is best-effort and is not relied upon.
 */
import type { ControlApi } from '../shared/api';
import type { ProxyController } from './proxy';
import type { StateStore } from '../shared/state';

export const POLL_INTERVAL_MS = 10_000;

/**
 * Consecutive *refused* checks before tripping. Unchanged at two: a refused
 * connection means nothing is listening, and one dropped check during a daemon
 * restart should still not tear down a working setup.
 */
export const REFUSED_THRESHOLD = 2;

/**
 * Consecutive checks of any kind before tripping — the ceiling that catches a
 * daemon which is timing out rather than refusing, and any mixture of the two.
 *
 * Five polls at ten seconds, with the budget below escalating each time, is
 * roughly a minute of a daemon that never answered. That is long enough that
 * "busy" is no longer a credible explanation, and short enough that a genuinely
 * dead daemon does not leave the browser pointed at nothing for long.
 */
export const UNRESPONSIVE_THRESHOLD = 5;

/** First timeout budget. */
export const HEALTH_TIMEOUT_MS = 3_000;

/**
 * Ceiling for the escalated budget.
 *
 * Each consecutive timeout doubles the budget, because the second check of a
 * busy daemon should be more patient than the first, not equally impatient.
 * Without this the poller asks the same question at the same deadline forever
 * and learns nothing new.
 */
export const HEALTH_TIMEOUT_MAX_MS = 12_000;

/** Why a check failed. The distinction is the entire point of this module. */
export type FailureKind = 'refused' | 'timeout';

/**
 * Classify a failed health check.
 *
 * An aborted fetch is our own timeout firing. Everything else reaching here is
 * a transport failure to loopback — refused, unreachable, or a socket error —
 * which for a port on this machine means nothing is listening.
 *
 * The imprecision is deliberate and safe in one direction: anything
 * misclassified as `refused` gets today's behaviour, and only `timeout` gets
 * the longer rope. This change cannot make the fail-safe trip sooner than it
 * already did.
 */
export function classifyFailure(error: unknown): FailureKind {
  if (error instanceof DOMException && error.name === 'AbortError') return 'timeout';
  if (error instanceof Error && error.name === 'AbortError') return 'timeout';
  return 'refused';
}

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
  private refusals = 0;
  private timeouts = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastOk: boolean | null = null;

  constructor(private readonly deps: HealthDeps) {}

  get consecutiveFailures(): number {
    return this.failures;
  }

  /** Consecutive refused checks — the definitive signal. */
  get consecutiveRefusals(): number {
    return this.refusals;
  }

  /**
   * The budget for the next check, escalating with consecutive timeouts.
   *
   * Exposed so a test can assert the escalation rather than infer it from
   * timing, which would make the test a clock measurement.
   */
  get timeoutBudgetMs(): number {
    return Math.min(HEALTH_TIMEOUT_MS * 2 ** this.timeouts, HEALTH_TIMEOUT_MAX_MS);
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
    this.refusals = 0;
    this.timeouts = 0;
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
      this.refusals = 0;
      this.timeouts = 0;
      return true;
    }

    const controller = new AbortController();
    const budget = this.timeoutBudgetMs;
    const timeout = setTimeout(() => controller.abort(), budget);
    let ok = false;
    // Only meaningful when ok is false. Defaulting to 'refused' means an
    // unexpected shape gets the stricter, pre-existing behaviour.
    let kind: FailureKind = 'refused';
    try {
      const health = await this.deps.api.health(controller.signal);
      ok = health.ok === true;
    } catch (error) {
      ok = false;
      kind = classifyFailure(error);
    } finally {
      clearTimeout(timeout);
    }

    this.lastOk = ok;

    if (ok) {
      const recovered = this.failures > 0;
      this.failures = 0;
      this.refusals = 0;
      this.timeouts = 0;
      if (recovered) await this.deps.onRecover?.();
      return true;
    }

    this.failures += 1;
    if (kind === 'refused') {
      this.refusals += 1;
      this.timeouts = 0;
    } else {
      this.timeouts += 1;
      this.refusals = 0;
    }

    // Two independent triggers, so a run of timeouts cannot hide behind the
    // refused counter and a flapping mixture of both still terminates.
    if (this.refusals >= REFUSED_THRESHOLD || this.failures >= UNRESPONSIVE_THRESHOLD) {
      await this.trip(this.refusals >= REFUSED_THRESHOLD ? 'refused' : 'timeout');
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
  private async trip(kind: FailureKind): Promise<void> {
    const state = await this.deps.store.load();
    if (!state.proxyEnabled && state.failSafeTrippedAt !== null) return;

    try {
      await this.deps.proxy.disable();
    } catch {
      /* If even clearing fails there is nothing further we can do from here,
         but the error state below still tells the user where to look. */
    }

    // Two causes, two messages. Telling someone to start a daemon that is
    // already running is the failure mode OI-18 was about: a confident, wrong
    // instruction costs more than no instruction, because it gets followed.
    const reason = kind === 'refused' ? 'daemon_unreachable' : 'daemon_unresponsive';
    const message =
      kind === 'refused'
        ? 'pporlock turned the proxy off because the daemon stopped responding. ' +
          'Your browsing is working again. Start it with `pporlock run`, then turn ' +
          'the proxy back on.'
        : 'pporlock turned the proxy off because the daemon stopped answering in ' +
          'time. It may still be running and simply overloaded — check it with ' +
          '`pporlock status` before restarting it. Your browsing is working again.';

    await this.deps.store.save({
      proxyEnabled: false,
      proxyApplied: false,
      failSafeTrippedAt: Date.now(),
      lastError: { code: reason, message, at: Date.now() },
    });

    await this.deps.onTrip?.(reason);
  }
}
