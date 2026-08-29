/** Messages between the popup and the service worker. */
import type { DurableState } from './state';

export type Message =
  | { type: 'get_status' }
  | { type: 'set_proxy'; enabled: boolean }
  | { type: 'pair'; code: string }
  | { type: 'activate_profile'; name: string }
  | { type: 'set_dev_toggle'; toggle: 'anticache' | 'anticomp'; value: boolean }
  | { type: 'bypass_host'; host: string }
  | { type: 'dismiss_error' }
  | { type: 'health_check' }
  | { type: 'suppress_host'; host: string }
  | { type: 'unsuppress_host'; host: string }
  | { type: 'set_banner_enabled'; enabled: boolean }
  | { type: 'set_proxy_scope'; scope: 'all' | 'scoped'; hosts?: string[] }
  | { type: 'start_recording'; name: string }
  | { type: 'stop_recording' };

export interface StatusReply {
  state: DurableState;
  /** Whether the optional <all_urls> grant needed for per-tab attribution is held. */
  attributionGranted: boolean;
  daemonReachable: boolean;
  proxyControllable: boolean;
  controlLevel: string;
  profiles: string[];
  counters: { flows: number; blocked: number; modified: number; passthrough: number } | null;
  /**
   * The **daemon's** version, from GET /state. Null when it is unreachable.
   *
   * Named for its source because the popup shows it beside the extension's own,
   * and the two are separately built and separately installed — the extension
   * is loaded unpacked and goes stale the moment it is rebuilt without being
   * reloaded. An unlabelled version number is then actively misleading: it
   * reports the half that was updated (OI-24).
   */
  version: string | null;
  /** This extension's own version, from its manifest. Always known. */
  extensionVersion: string;
}

export interface ActionReply {
  ok: boolean;
  error?: string;
  state?: DurableState;
}
