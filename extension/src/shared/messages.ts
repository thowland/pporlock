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
  | { type: 'health_check' };

export interface StatusReply {
  state: DurableState;
  /** Whether the optional <all_urls> grant needed for per-tab attribution is held. */
  attributionGranted: boolean;
  daemonReachable: boolean;
  proxyControllable: boolean;
  controlLevel: string;
  profiles: string[];
  counters: { flows: number; blocked: number; modified: number; passthrough: number } | null;
  version: string | null;
}

export interface ActionReply {
  ok: boolean;
  error?: string;
  state?: DurableState;
}
