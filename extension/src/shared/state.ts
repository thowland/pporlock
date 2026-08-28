/**
 * Service-worker state (SPEC-3 §3.1).
 *
 * MV3 service workers are terminated aggressively. The design consequence is
 * absolute: **nothing lives only in the worker's memory.**
 *
 *   chrome.storage.local    durable — proxy on/off, token, paired status, and
 *                           the settings the user chose
 *   chrome.storage.session  ephemeral — per-tab counters and the attribution
 *                           buffer. Survives a worker restart but not a browser
 *                           restart, which is exactly the right lifetime for
 *                           both.
 */

export type ExtErrorCode =
  | 'daemon_unreachable'
  | 'unpaired'
  | 'token_rejected'
  | 'proxy_not_controllable'
  | 'proxy_set_failed'
  | 'attribution_overflow'
  | 'sse_disconnected';

export interface ExtError {
  code: ExtErrorCode;
  message: string;
  at: number;
  detail?: Record<string, unknown>;
}

export interface DurableState {
  /** Whether the user has asked for the proxy to be on. */
  proxyEnabled: boolean;
  /** Whether the extension actually holds Chrome's proxy configuration. */
  proxyApplied: boolean;
  paired: boolean;
  token: string | null;
  controlOrigin: string;
  activeProfile: string | null;
  devToggles: { anticache: boolean; anticomp: boolean };
  moduleHealth: { errors: number; quarantined: number };
  recordingSession: string | null;
  lastError: ExtError | null;
  /** Set when the fail-safe cleared the proxy. Never auto-cleared. */
  failSafeTrippedAt: number | null;
}

export const DEFAULT_CONTROL_ORIGIN = 'http://127.0.0.1:8081';

export const DEFAULT_STATE: DurableState = {
  proxyEnabled: false,
  proxyApplied: false,
  paired: false,
  token: null,
  controlOrigin: DEFAULT_CONTROL_ORIGIN,
  activeProfile: null,
  devToggles: { anticache: false, anticomp: false },
  moduleHealth: { errors: 0, quarantined: 0 },
  recordingSession: null,
  lastError: null,
  failSafeTrippedAt: null,
};

const STATE_KEY = 'pporlock.state';

/** Per-tab tallies. Session-scoped: a browser restart should not carry them. */
export interface TabCounters {
  requests: number;
  blocked: number;
  modified: number;
  warnings: number;
  errors: number;
}

export const EMPTY_COUNTERS: TabCounters = {
  requests: 0,
  blocked: 0,
  modified: 0,
  warnings: 0,
  errors: 0,
};

export interface StorageArea {
  get(keys: string | string[] | null): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
  remove(keys: string | string[]): Promise<void>;
}

export class StateStore {
  constructor(
    private readonly local: StorageArea,
    private readonly session: StorageArea,
  ) {}

  async load(): Promise<DurableState> {
    const stored = await this.local.get(STATE_KEY);
    // STATE_KEY is a module constant, not user input.
    // eslint-disable-next-line security/detect-object-injection
    const value = stored[STATE_KEY];
    if (!value || typeof value !== 'object') return { ...DEFAULT_STATE };
    // Merge over defaults so a state written by an older version, missing keys
    // added since, does not produce undefined fields the popup then renders.
    return { ...DEFAULT_STATE, ...(value as Partial<DurableState>) };
  }

  async save(patch: Partial<DurableState>): Promise<DurableState> {
    const next = { ...(await this.load()), ...patch };
    await this.local.set({ [STATE_KEY]: next });
    return next;
  }

  async clearToken(): Promise<DurableState> {
    return this.save({ token: null, paired: false });
  }

  // -- per-tab counters ------------------------------------------------

  private tabKey(tabId: number): string {
    return `pporlock.tab.${tabId}`;
  }

  async getCounters(tabId: number): Promise<TabCounters> {
    const key = this.tabKey(tabId);
    const stored = await this.session.get(key);
    // security/detect-object-injection: `key` is built by tabKey() from a
    // numeric tab id, and `stored` is a fresh object we just received. There is
    // no path from user input to a property name here.
    // eslint-disable-next-line security/detect-object-injection
    const value = stored[key];
    if (!value || typeof value !== 'object') return { ...EMPTY_COUNTERS };
    return { ...EMPTY_COUNTERS, ...(value as Partial<TabCounters>) };
  }

  async bumpCounters(tabId: number, delta: Partial<TabCounters>): Promise<TabCounters> {
    const current = await this.getCounters(tabId);
    const next: TabCounters = {
      requests: current.requests + (delta.requests ?? 0),
      blocked: current.blocked + (delta.blocked ?? 0),
      modified: current.modified + (delta.modified ?? 0),
      warnings: current.warnings + (delta.warnings ?? 0),
      errors: current.errors + (delta.errors ?? 0),
    };
    await this.session.set({ [this.tabKey(tabId)]: next });
    return next;
  }

  async resetCounters(tabId: number): Promise<void> {
    await this.session.remove(this.tabKey(tabId));
  }
}

/** Adapts chrome.storage to the StorageArea interface used above. */
export function chromeArea(area: chrome.storage.StorageArea): StorageArea {
  return {
    get: (keys) => area.get(keys),
    set: (items) => area.set(items),
    remove: (keys) => area.remove(keys),
  };
}
