/**
 * The service worker (SPEC-3 §3).
 *
 * Terminated aggressively by MV3, so it holds no authoritative state: every
 * handler reloads from storage. The alarm is what lets a suspended worker still
 * notice that the daemon has died — which is the entire point of the fail-safe.
 */
import { ControlApi } from '../shared/api';
import type { ActionReply, Message, StatusReply } from '../shared/messages';
import { StateStore, chromeArea, DEFAULT_CONTROL_ORIGIN } from '../shared/state';
import { applyBadge, badgeView, chromeBadgeApi, resolveBadgeState } from './badge';
import { HealthMonitor, POLL_INTERVAL_MS } from './health';
import { ProxyController, chromeProxyApi } from './proxy';

const ALARM_NAME = 'pporlock.health';
/** chrome.alarms enforces a one-minute floor; the timer covers the gap. */
const ALARM_PERIOD_MINUTES = 1;

const store = new StateStore(chromeArea(chrome.storage.local), chromeArea(chrome.storage.session));
const proxy = new ProxyController(chromeProxyApi());
const badge = chromeBadgeApi();

let api = new ControlApi(DEFAULT_CONTROL_ORIGIN);

async function apiForState(): Promise<ControlApi> {
  const state = await store.load();
  if (api.origin !== state.controlOrigin) {
    api = new ControlApi(state.controlOrigin);
  }
  api.setToken(state.token);
  return api;
}

const health = new HealthMonitor({
  api,
  proxy,
  store,
  onTrip: async () => {
    await refreshBadge();
    try {
      // A badge alone is too quiet for "your proxy just turned itself off".
      await chrome.notifications?.create?.({
        type: 'basic',
        iconUrl: 'icon-128.png',
        title: 'pporlock turned the proxy off',
        message:
          'The daemon stopped responding, so Chrome was returned to a direct ' +
          'connection. Start it with `pporlock run`.',
      });
    } catch {
      /* notifications permission is optional; the badge still reports it */
    }
  },
  onRecover: refreshBadge,
});

async function refreshBadge(): Promise<void> {
  const state = await store.load();
  const view = badgeView(
    resolveBadgeState({
      proxyEnabled: state.proxyEnabled,
      failSafeTripped: state.failSafeTrippedAt !== null,
      daemonReachable: health.healthy !== false,
      devToggleActive: state.devToggles.anticache || state.devToggles.anticomp,
      counts: { blocked: 0, modified: 0, warnings: 0, errors: 0 },
    }),
  );
  await applyBadge(badge, view);
}

function proxyTargetFrom(listen: string | undefined): { host: string; port: number } {
  // The daemon reports where it listens; trusting that avoids a second place
  // for the port to be configured and get out of step.
  const fallback = { host: '127.0.0.1', port: 8080 };
  if (!listen) return fallback;
  const [host, port] = listen.split(':');
  if (!host || !port) return fallback;
  const parsed = Number.parseInt(port, 10);
  return Number.isFinite(parsed) ? { host, port: parsed } : fallback;
}

async function enableProxy(): Promise<ActionReply> {
  const client = await apiForState();
  const state = await store.load();

  if (!(await proxy.isControllable())) {
    const { level } = await proxy.status();
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
  } catch {
    return { ok: false, error: 'Cannot reach the daemon. Start it with `pporlock run`.' };
  }

  try {
    await proxy.enable(proxyTargetFrom(listen), state.controlOrigin);
  } catch (error) {
    return { ok: false, error: `Could not set the proxy: ${String(error)}` };
  }

  health.reset();
  const next = await store.save({
    proxyEnabled: true,
    proxyApplied: true,
    failSafeTrippedAt: null,
    lastError: null,
  });
  health.start(POLL_INTERVAL_MS);
  await refreshBadge();
  return { ok: true, state: next };
}

async function disableProxy(): Promise<ActionReply> {
  try {
    await proxy.disable();
  } catch (error) {
    return { ok: false, error: `Could not clear the proxy: ${String(error)}` };
  }
  health.stop();
  health.reset();
  const next = await store.save({ proxyEnabled: false, proxyApplied: false });
  await refreshBadge();
  return { ok: true, state: next };
}

async function status(): Promise<StatusReply> {
  const state = await store.load();
  const client = await apiForState();
  const { level } = await proxy.status();

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
    await store.save({
      activeProfile: daemon.active_profile,
      devToggles: daemon.dev_toggles,
      moduleHealth: {
        errors: daemon.modules.errors.length,
        quarantined: daemon.modules.quarantined,
      },
      recordingSession: daemon.capture.recording_session,
    });
  } catch {
    try {
      daemonReachable = (await client.health()).ok;
    } catch {
      daemonReachable = false;
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
    state: await store.load(),
    daemonReachable,
    proxyControllable:
      level === 'controllable_by_this_extension' || level === 'controlled_by_this_extension',
    controlLevel: level,
    profiles,
    counters,
    version,
  };
}

async function handle(message: Message): Promise<ActionReply | StatusReply> {
  switch (message.type) {
    case 'get_status':
      return status();

    case 'set_proxy':
      return message.enabled ? enableProxy() : disableProxy();

    case 'pair': {
      const client = await apiForState();
      try {
        const { token } = await client.pair(message.code);
        const next = await store.save({ token, paired: true, lastError: null });
        return { ok: true, state: next };
      } catch (error) {
        return { ok: false, error: String((error as Error).message ?? error) };
      }
    }

    case 'activate_profile': {
      const client = await apiForState();
      try {
        const daemon = await client.activateProfile(message.name);
        return { ok: true, state: await store.save({ activeProfile: daemon.active_profile }) };
      } catch (error) {
        return { ok: false, error: String((error as Error).message ?? error) };
      }
    }

    case 'set_dev_toggle': {
      const client = await apiForState();
      try {
        const daemon = await client.setDevToggles({ [message.toggle]: message.value });
        const next = await store.save({ devToggles: daemon.dev_toggles });
        await refreshBadge();
        return { ok: true, state: next };
      } catch (error) {
        return { ok: false, error: String((error as Error).message ?? error) };
      }
    }

    case 'bypass_host': {
      const client = await apiForState();
      try {
        const current = await client.getExclusions();
        await client.putExclusions([
          ...current.entries,
          { pattern: message.host, comment: 'added from the extension popup' },
        ]);
        return { ok: true };
      } catch (error) {
        return { ok: false, error: String((error as Error).message ?? error) };
      }
    }

    case 'dismiss_error':
      return { ok: true, state: await store.save({ lastError: null, failSafeTrippedAt: null }) };

    case 'health_check':
      return { ok: await health.check() };
  }
}

chrome.runtime.onMessage.addListener((message: Message, _sender, sendResponse) => {
  void handle(message)
    .then(sendResponse)
    .catch((error: unknown) => sendResponse({ ok: false, error: String(error) }));
  return true; // keep the channel open for the async reply
});

// The alarm is what lets a suspended worker still notice a dead daemon.
chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_PERIOD_MINUTES });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) void health.check().then(refreshBadge);
});

// On every wake, re-establish whatever the durable state says should be running.
void (async () => {
  const state = await store.load();
  if (state.proxyEnabled) health.start(POLL_INTERVAL_MS);
  await refreshBadge();
})();

chrome.tabs.onRemoved.addListener((tabId) => void store.resetCounters(tabId));
