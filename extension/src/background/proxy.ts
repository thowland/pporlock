/**
 * Chrome proxy control (SPEC-3 §4).
 *
 * This and tab attribution are the two things nothing but an extension can do.
 * Only Chrome is affected; the daemon never touches macOS system proxy settings
 * (REQ SCP-001).
 */
import { DEFAULT_CONTROL_ORIGIN } from '../shared/state';

export type ControlLevel =
  | 'controllable_by_this_extension'
  | 'controlled_by_this_extension'
  | 'controlled_by_other_extensions'
  | 'not_controllable'
  | 'controlled_by_policy'
  | 'unknown';

export interface ProxyTarget {
  host: string;
  port: number;
}

export interface ProxyStatus {
  level: ControlLevel;
  ours: boolean;
}

/**
 * Hosts that must never route through the proxy.
 *
 * The control origin above all: without it the extension's own API calls and
 * the fail-safe health check would go through the proxy, so a dead proxy would
 * make the check fail in a way indistinguishable from a dead daemon — and the
 * fail-safe would have no way to tell them apart (SPEC-3 §4.4 rule 5).
 */
export function bypassList(controlOrigin: string = DEFAULT_CONTROL_ORIGIN): string[] {
  const base = ['127.0.0.1', 'localhost', '[::1]', '<local>'];
  try {
    const url = new URL(controlOrigin);
    const entry = url.port ? `${url.hostname}:${url.port}` : url.hostname;
    if (!base.includes(entry)) base.push(entry);
  } catch {
    /* a malformed origin is caught at construction; the defaults still hold */
  }
  return base;
}

export function fixedServerConfig(
  target: ProxyTarget,
  controlOrigin?: string,
): chrome.proxy.ProxyConfig {
  return {
    mode: 'fixed_servers',
    rules: {
      singleProxy: { scheme: 'http', host: target.host, port: target.port },
      bypassList: bypassList(controlOrigin),
    },
  };
}

/**
 * A PAC script scoping the proxy to named hosts (REQ EXT-003).
 *
 * Off by default; fixed-server is the normal path. Useful when only certain
 * sites should be proxied at all.
 */
export function pacScript(includeHosts: string[], target: ProxyTarget): string {
  // Everything interpolated goes through JSON.stringify, including the proxy
  // target. The host comes from the daemon's reported listen address, which is
  // not attacker-controlled today — but a PAC script is executable source, and
  // "the input happens to be safe right now" is not a property worth depending
  // on when encoding it correctly costs nothing.
  const patterns = JSON.stringify(includeHosts);
  const proxy = JSON.stringify(`PROXY ${target.host}:${target.port}`);
  return `function FindProxyForURL(url, host) {
  if (isPlainHostName(host) || host === "127.0.0.1" || host === "localhost") return "DIRECT";
  var include = ${patterns};
  if (include.length === 0) return ${proxy};
  for (var i = 0; i < include.length; i++) {
    if (shExpMatch(host, include[i])) return ${proxy};
  }
  return "DIRECT";
}`;
}

export interface ProxyApi {
  set(config: { value: chrome.proxy.ProxyConfig; scope: string }): Promise<void>;
  clear(config: { scope: string }): Promise<void>;
  get(config: { incognito?: boolean }): Promise<{ levelOfControl: string }>;
}

export class ProxyController {
  constructor(private readonly api: ProxyApi) {}

  async status(): Promise<ProxyStatus> {
    try {
      const current = await this.api.get({});
      const level = current.levelOfControl as ControlLevel;
      return { level, ours: level === 'controlled_by_this_extension' };
    } catch {
      return { level: 'unknown', ours: false };
    }
  }

  /**
   * Can we set the proxy at all?
   *
   * Another extension or an enterprise policy may hold it. Detecting that and
   * saying which lets the popup explain the situation instead of showing an
   * enabled state the extension does not actually own (SPEC-3 §4.3).
   */
  async isControllable(): Promise<boolean> {
    const { level } = await this.status();
    return level === 'controllable_by_this_extension' || level === 'controlled_by_this_extension';
  }

  async enable(target: ProxyTarget, controlOrigin?: string): Promise<void> {
    await this.api.set({ value: fixedServerConfig(target, controlOrigin), scope: 'regular' });
  }

  async enablePac(includeHosts: string[], target: ProxyTarget): Promise<void> {
    await this.api.set({
      value: { mode: 'pac_script', pacScript: { data: pacScript(includeHosts, target) } },
      scope: 'regular',
    });
  }

  async disable(): Promise<void> {
    await this.api.clear({ scope: 'regular' });
  }
}

/** The real chrome.proxy, adapted. Kept out of the class so it can be faked. */
export function chromeProxyApi(): ProxyApi {
  // chrome.proxy.settings is typed as callback-style in @types/chrome but
  // returns promises in MV3. The casts are confined here, which is the whole
  // reason ProxyController takes an interface rather than touching chrome.*.
  return {
    set: (config) => chrome.proxy.settings.set(config as never) as unknown as Promise<void>,
    clear: (config) => chrome.proxy.settings.clear(config as never) as unknown as Promise<void>,
    get: (config) =>
      chrome.proxy.settings.get(config as never) as unknown as Promise<{
        levelOfControl: string;
      }>,
  };
}
