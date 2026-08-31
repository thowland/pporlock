/**
 * The service worker's daemon-facing decisions, separated from its wiring.
 *
 * They live here for one reason: `index.ts` registers chrome listeners at
 * module load, so it cannot be imported by a test, and it is excluded from
 * coverage. Two of the four bugs in OI-34 were inline in that file — not
 * plumbing, but decisions about what a failure *means* — and neither could be
 * pinned while it stayed there. A component that opts out of testing collects
 * exactly the class of bug CLAUDE.md's lesson 1 describes.
 *
 * Everything here takes its collaborators as `ActionDeps` rather than reaching
 * for a module-level singleton, so a test can drive the real function against a
 * real ControlApi and a stubbed `fetch` — the daemon's actual wire response,
 * not our client's beliefs about it (lesson 2).
 */
import type { ControlApi } from '../shared/api';
import { classifyApiError, daemonFailureMessage, remedyFor } from '../shared/errors';
import type { ActionReply, StatusReply } from '../shared/messages';
import type { ExtErrorCode, StateStore } from '../shared/state';
import type { HealthMonitor } from './health';
import type { ProxyController } from './proxy';

export interface ActionDeps {
  /** Resolved per call: the control origin is a setting and can change. */
  client: () => Promise<ControlApi>;
  store: StateStore;
  proxy: ProxyController;
  health: HealthMonitor;
  refreshBadge: () => Promise<void>;
  attributionGranted: () => Promise<boolean>;
  /** The extension's own version, for the popup's "which am I running". */
  extensionVersion: () => string;
  pollIntervalMs: number;
}

export function proxyTargetFrom(listen: string | undefined): { host: string; port: number } {
  // The daemon reports where it listens; trusting that avoids a second place
  // for the port to be configured and get out of step.
  const fallback = { host: '127.0.0.1', port: 8080 };
  if (!listen) return fallback;
  const [host, port] = listen.split(':');
  if (!host || !port) return fallback;
  const parsed = Number.parseInt(port, 10);
  return Number.isFinite(parsed) ? { host, port: parsed } : fallback;
}

/**
 * Record a daemon refusal, and stop claiming to be paired.
 *
 * Returns the code when the daemon answered with one, and null when the call
 * failed for any other reason — callers need that distinction, because "the
 * daemon refused me" and "the daemon is not there" have opposite remedies and
 * used to be reported as the same thing.
 *
 * `paired` is dropped here as well as in the health monitor because the popup
 * can provoke a refusal on any action, and a stale `paired: true` is what hides
 * the pairing prompt that would fix it.
 */
export async function noteAuthFailure(
  deps: ActionDeps,
  error: unknown,
): Promise<ExtErrorCode | null> {
  const code = classifyApiError(error);
  if (code === null) return null;
  await deps.store.save({
    paired: false,
    lastError: { code, message: remedyFor(code), at: Date.now() },
  });
  await deps.refreshBadge();
  return code;
}

/**
 * The message for a failed daemon call: the remedy for a refusal, and the
 * error's own text for anything else. Never the bare wire message for a 401 or
 * 403 — "origin not permitted" is true and tells the user nothing they can act
 * on (REQ EXT-024).
 */
export async function failureMessage(deps: ActionDeps, error: unknown): Promise<string> {
  const code = await noteAuthFailure(deps, error);
  if (code !== null) return remedyFor(code);
  return String((error as Error)?.message ?? error);
}

export async function enableProxy(deps: ActionDeps): Promise<ActionReply> {
  const client = await deps.client();
  const state = await deps.store.load();

  if (!(await deps.proxy.isControllable())) {
    const { level } = await deps.proxy.status();
    return {
      ok: false,
      error:
        level === 'controlled_by_policy'
          ? 'Chrome’s proxy is controlled by an enterprise policy.'
          : 'Another extension is controlling Chrome’s proxy.',
    };
  }

  let listen: string | undefined;
  try {
    listen = (await client.getState()).proxy.listen;
  } catch (error) {
    // This catch used to swallow everything into "cannot reach the daemon",
    // which is how an unpaired extension sent people to restart a daemon that
    // was running fine — at the exact moment they were looking for the reason.
    await noteAuthFailure(deps, error);
    return { ok: false, error: daemonFailureMessage(error) };
  }

  try {
    // Scoped mode is a PAC script rather than a bypass list: bypassList only
    // subtracts from "everything", and scoping needs the opposite default.
    if (state.proxyScope === 'scoped') {
      await deps.proxy.enablePac(state.scopedHosts, proxyTargetFrom(listen));
    } else {
      await deps.proxy.enable(proxyTargetFrom(listen), state.controlOrigin);
    }
  } catch (error) {
    return { ok: false, error: `Could not set the proxy: ${String(error)}` };
  }

  deps.health.reset();
  const next = await deps.store.save({
    proxyEnabled: true,
    proxyApplied: true,
    failSafeTrippedAt: null,
    lastError: null,
  });
  deps.health.start(deps.pollIntervalMs);
  await deps.refreshBadge();
  return { ok: true, state: next };
}

export async function disableProxy(deps: ActionDeps): Promise<ActionReply> {
  try {
    await deps.proxy.disable();
  } catch (error) {
    return { ok: false, error: `Could not clear the proxy: ${String(error)}` };
  }
  deps.health.stop();
  deps.health.reset();
  const next = await deps.store.save({ proxyEnabled: false, proxyApplied: false });
  await deps.refreshBadge();
  return { ok: true, state: next };
}

export async function status(deps: ActionDeps): Promise<StatusReply> {
  const state = await deps.store.load();
  const client = await deps.client();
  const { level } = await deps.proxy.status();

  let daemonReachable = false;
  let version: string | null = null;
  let profiles: string[] = [];
  let counters: StatusReply['counters'] = null;

  try {
    const daemon = await client.getState();
    daemonReachable = true;
    version = daemon.version;
    counters = {
      flows: daemon.counters.flows_total,
      blocked: daemon.counters.blocked,
      modified: daemon.counters.modified,
      passthrough: daemon.counters.passthrough,
    };
    // Keep the mirrored state honest so the popup never shows a stale toggle.
    await deps.store.save({
      activeProfile: daemon.active_profile,
      devToggles: daemon.dev_toggles,
      moduleHealth: {
        errors: daemon.modules.errors.length,
        quarantined: daemon.modules.quarantined,
      },
      recordingSession: daemon.capture.recording_session,
    });
  } catch (error) {
    // A refusal is proof the daemon is up: it answered. Falling through to the
    // health probe would only collect the same 403 and score it as "down",
    // which suppresses the popup's pairing prompt — the one control that fixes
    // it. Reachability and usability are different questions.
    if ((await noteAuthFailure(deps, error)) !== null) {
      daemonReachable = true;
    } else {
      try {
        daemonReachable = (await client.health()).ok;
      } catch {
        daemonReachable = false;
      }
    }
  }

  if (daemonReachable && state.paired) {
    try {
      profiles = (await client.listProfiles()).map((p) => p.name);
    } catch {
      profiles = [];
    }
  }

  return {
    state: await deps.store.load(),
    attributionGranted: await deps.attributionGranted(),
    daemonReachable,
    proxyControllable:
      level === 'controllable_by_this_extension' || level === 'controlled_by_this_extension',
    controlLevel: level,
    profiles,
    counters,
    version,
    extensionVersion: deps.extensionVersion(),
  };
}
