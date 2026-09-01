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
import { type ActionDeps, disableProxy, enableProxy, failureMessage, status } from './actions';
import { Attributor } from './attribution';
import { applyBadge, badgeView, chromeBadgeApi, resolveBadgeState } from './badge';
import { describeLights, iconLights, renderIcon } from './icon';
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
    // Follow the control origin so we never attribute our own API traffic.
    attributor.setIgnoreOrigin(state.controlOrigin);
  }
  api.setToken(state.token);
  return api;
}

/**
 * Tab attribution. Submits through whatever ControlApi is current, so it
 * follows a control-origin change without being rebuilt.
 */
const attributor = new Attributor(
  {
    submit: async (entries) => {
      const client = await apiForState();
      await client.submitAttribution(entries);
    },
  },
  undefined,
  DEFAULT_CONTROL_ORIGIN,
);

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
        iconUrl: 'icons/poppy-128.png',
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
  // A refusal changes what the popup must offer, so the badge follows it too.
  onRejected: () => refreshBadge(),
});

async function refreshBadge(tabId?: number): Promise<void> {
  const state = await store.load();
  // Per-tab counts once attribution can supply them; the global badge stays as
  // the fallback for a tab we have nothing for.
  const counters =
    tabId === undefined
      ? { blocked: 0, modified: 0, warnings: 0, errors: 0 }
      : await store.getCounters(tabId);

  const view = badgeView(
    resolveBadgeState({
      proxyEnabled: state.proxyEnabled,
      failSafeTripped: state.failSafeTrippedAt !== null,
      daemonReachable: health.reachable,
      devToggleActive: state.devToggles.anticache || state.devToggles.anticomp,
      counts: counters,
    }),
    counters,
  );

  // The lamps say what is true right now; the badge says what has happened.
  // Both are wanted at once, which is why they are not the same surface.
  const lights = iconLights({
    proxyEnabled: state.proxyEnabled,
    proxyApplied: state.proxyApplied,
    daemonReachable: health.reachable,
    failSafeTripped: state.failSafeTrippedAt !== null,
    recording: state.recordingSession !== null,
  });

  await applyBadge(badge, { ...view, title: `${view.title} — ${describeLights(lights)}` }, tabId);

  // An icon that could not be drawn must never take down the badge refresh:
  // the badge is carrying the more important message, and a stale icon is a
  // far smaller failure than a fail-safe trip that goes unannounced.
  try {
    await renderIcon(lights, tabId);
  } catch {
    /* the packaged PNG remains, unlit */
  }
}

/**
 * What the action functions are allowed to reach for. Assembled here because
 * this is the only place that may touch `chrome.*` at module scope; everything
 * in actions.ts takes it as an argument and is therefore testable.
 */
const deps: ActionDeps = {
  client: apiForState,
  store,
  proxy,
  health,
  refreshBadge: () => refreshBadge(),
  attributionGranted: hasAttributionPermission,
  // version_name carries the full semver; `version` is the numeric core Chrome
  // will store. Prefer the former so a prerelease is visible to the person
  // trying to work out what they are running (OI-25).
  extensionVersion: () =>
    chrome.runtime.getManifest().version_name ?? chrome.runtime.getManifest().version,
  pollIntervalMs: POLL_INTERVAL_MS,
};

async function handle(message: Message): Promise<ActionReply | StatusReply> {
  switch (message.type) {
    case 'get_status':
      return status(deps);

    case 'set_proxy':
      return message.enabled ? enableProxy(deps) : disableProxy(deps);

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
        return { ok: false, error: await failureMessage(deps, error) };
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
        return { ok: false, error: await failureMessage(deps, error) };
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
        return { ok: false, error: await failureMessage(deps, error) };
      }
    }

    case 'suppress_host': {
      const state = await store.load();
      const hosts = new Set(state.suppressedHosts);
      hosts.add(message.host);
      return { ok: true, state: await store.save({ suppressedHosts: [...hosts] }) };
    }

    case 'unsuppress_host': {
      const state = await store.load();
      return {
        ok: true,
        state: await store.save({
          suppressedHosts: state.suppressedHosts.filter((h) => h !== message.host),
        }),
      };
    }

    case 'set_proxy_scope': {
      const next = await store.save({
        proxyScope: message.scope,
        ...(message.hosts ? { scopedHosts: message.hosts } : {}),
      });
      // Re-apply immediately if the proxy is already on, otherwise the setting
      // silently does nothing until the next toggle.
      if (next.proxyApplied) {
        const reply = await enableProxy(deps);
        if (!reply.ok) return reply;
      }
      return { ok: true, state: await store.load() };
    }

    case 'start_recording': {
      const client = await apiForState();
      try {
        const meta = await client.startSession(message.name);
        const next = await store.save({ recordingSession: meta.session_id });
        await refreshBadge();
        return { ok: true, state: next };
      } catch (error) {
        return { ok: false, error: await failureMessage(deps, error) };
      }
    }

    case 'stop_recording': {
      const state = await store.load();
      if (state.recordingSession === null) return { ok: false, error: 'Not recording.' };
      const client = await apiForState();
      try {
        await client.stopSession(state.recordingSession);
      } catch (error) {
        // The session may already have been stopped from the web UI. Clearing
        // local state anyway is right: leaving the popup claiming to record
        // something that is not recording is the worse failure.
        await store.save({ recordingSession: null });
        await refreshBadge();
        return { ok: false, error: await failureMessage(deps, error) };
      }
      const next = await store.save({ recordingSession: null });
      await refreshBadge();
      return { ok: true, state: next };
    }

    case 'set_banner_enabled':
      return { ok: true, state: await store.save({ bannerEnabled: message.enabled }) };

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
  if (alarm.name === ALARM_NAME) void health.check().then(() => refreshBadge());
});

// On every wake, re-establish whatever the durable state says should be running.
void (async () => {
  const state = await store.load();
  if (state.proxyEnabled) health.start(POLL_INTERVAL_MS);
  await refreshBadge();
})();

chrome.tabs.onRemoved.addListener((tabId) => void store.resetCounters(tabId));

/**
 * Tell a page when pporlock changed it (REQ EXT-020).
 *
 * Driven from the daemon's own record rather than guessed at: the notes on the
 * document flow are the authority on what was done to it. Polled on navigation
 * completion, because that is when the document flow exists and the page is
 * ready to receive a message.
 */
export async function maybeWarnTab(tabId: number, url: string): Promise<void> {
  const state = await store.load();
  if (!state.bannerEnabled || !state.paired) return;

  let host = '';
  try {
    host = new URL(url).hostname;
  } catch {
    return;
  }
  if (state.suppressedHosts.includes(host)) return;

  try {
    const client = await apiForState();

    // Per-tab first, by host second.
    //
    // Tab attribution needs the optional <all_urls> grant — without it every
    // flow carries a null tab_id (the OI-2 spike measured 0% coverage), so a
    // tab-scoped query returns nothing and the banner would simply never
    // appear. Making the warning depend on a permission the user is told is
    // optional would silently disable the one affordance that exists to stop
    // this system modifying pages invisibly.
    //
    // The host fallback is broader than the tab: it can warn about a
    // modification made for a different tab on the same host. That is the
    // right way to be wrong — the modification did happen, and this host is
    // where it happened.
    let page = await client.listFlows({ tab_id: tabId, limit: 100 });
    if (page.flows.length === 0) {
      page = await client.listFlows({ host, limit: 100 });
    }

    // Every flow, not just the document. A stripped integrity
    // attribute on a subresource, or a module that errored handling one, is a
    // modification to *this page* as far as the person looking at it is
    // concerned — and it is exactly the kind that is otherwise invisible.
    const seen = new Set<string>();
    const notes = [];
    for (const flow of page.flows) {
      for (const note of flow.provenance?.notes ?? []) {
        if (note.severity !== 'warning' && note.severity !== 'error') continue;
        // Deduped by code and module: one line per distinct thing that
        // happened, however many subresources it happened to.
        const key = `${note.code}\u0000${note.module ?? ''}`;
        if (seen.has(key)) continue;
        seen.add(key);
        notes.push(note);
      }
    }
    if (notes.length === 0) return;

    const document_ = page.flows.find(
      (f) => f.request?.dest === 'document' || f.request?.url === url,
    );

    await chrome.tabs.sendMessage(tabId, {
      type: 'pporlock_banner',
      flowId: document_?.flow_id ?? '',
      notes,
      profile: state.activeProfile ?? 'default',
    });
  } catch {
    // The tab may have navigated away, or the content script may not be there.
    // A missing warning is a lesser failure than a thrown error in the worker.
  }
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url) {
    void maybeWarnTab(tabId, tab.url);
  }
});

/**
 * Attribution: observe only, never block — and only with an explicit grant.
 *
 * chrome.webRequest reports only requests the extension has host access to, so
 * this needs <all_urls>, which is optional and not granted at install
 * (see manifest.config.ts). Registering the listener without the grant would
 * silently produce nothing, which is worse than not registering it: the feature
 * would appear on and do nothing.
 *
 * The listener itself records and returns. Anything slower would add latency to
 * every request the browser makes, which is precisely the cost the daemon's own
 * pipeline works to avoid.
 */
let attributionListening = false;

function startAttribution(): void {
  if (attributionListening) return;
  attributionListening = true;

  chrome.webRequest.onBeforeRequest.addListener(
    (details) => {
      attributor.observe(details);
    },
    { urls: ['http://*/*', 'https://*/*'] },
  );

  // A navigation starts a new page, so its tally starts again.
  chrome.webRequest.onBeforeRequest.addListener(
    (details) => {
      if (details.type === 'main_frame' && details.tabId >= 0) {
        void store.resetCounters(details.tabId).then(() => refreshBadge(details.tabId));
      }
    },
    { urls: ['http://*/*', 'https://*/*'], types: ['main_frame'] },
  );
}

export async function hasAttributionPermission(): Promise<boolean> {
  try {
    return await chrome.permissions.contains({ origins: ['<all_urls>'] });
  } catch {
    return false;
  }
}

void hasAttributionPermission().then((granted) => {
  if (granted) startAttribution();
});

// Granting the permission takes effect at once rather than at the next restart.
chrome.permissions.onAdded?.addListener((granted) => {
  if (granted.origins?.includes('<all_urls>')) startAttribution();
});
