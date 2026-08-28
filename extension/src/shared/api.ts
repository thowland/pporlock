/**
 * Control API client for the extension (SPEC-3 §3.2).
 *
 * A deliberate subset. Two omissions are load-bearing:
 *
 *  - There is no unmask method. Unmasking is web-UI only (REQ CAP-043), and an
 *    extension that could not unmask cannot be made to leak by a bug.
 *  - The token is never placed in a URL, only in an Authorization header.
 */
import { assertLoopbackOrigin } from './control-origin';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export interface Health {
  ok: boolean;
  version: string;
}

export interface Profile {
  name: string;
  description?: string;
  modules?: string[];
}

export interface DaemonState {
  version: string;
  proxy: { running: boolean; listen: string; uptime_s: number };
  active_profile: string;
  dev_toggles: { anticache: boolean; anticomp: boolean };
  modules: { loaded: number; enabled: number; quarantined: number; errors: unknown[] };
  capture: { ring_flows: number; ring_bytes: number; recording_session: string | null };
  counters: {
    flows_total: number;
    blocked: number;
    modified: number;
    passthrough: number;
    errors: number;
  };
}

export class ControlApi {
  readonly origin: string;
  private token: string | null = null;

  constructor(origin: string) {
    this.origin = assertLoopbackOrigin(origin);
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  private async request<T>(
    path: string,
    { method = 'GET', body, auth = true }: { method?: string; body?: unknown; auth?: boolean } = {},
  ): Promise<T> {
    const headers: Record<string, string> = {};
    if (auth && this.token) headers['Authorization'] = `Bearer ${this.token}`;
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      // REQ API-013 — the header a cross-origin form cannot produce.
      headers['X-Pporlock-Client'] = 'extension';
    }

    const init: RequestInit = { method, headers };
    if (body !== undefined) init.body = JSON.stringify(body);

    const response = await fetch(`${this.origin}${path}`, init);
    if (response.status === 204) return undefined as T;

    if (!response.ok) {
      let code = `http_${response.status}`;
      let message = response.statusText;
      try {
        const payload = (await response.json()) as {
          error?: { code?: string; message?: string };
        };
        code = payload.error?.code ?? code;
        message = payload.error?.message ?? message;
      } catch {
        /* a non-JSON error body is still an error */
      }
      throw new ApiError(response.status, code, message);
    }
    return (await response.json()) as T;
  }

  /**
   * Liveness. Unauthenticated by design and kept cheap, because the fail-safe
   * polls it to decide whether to clear Chrome's proxy configuration.
   */
  health(signal?: AbortSignal): Promise<Health> {
    return this.requestWithSignal<Health>('/state/health', signal);
  }

  private async requestWithSignal<T>(path: string, signal?: AbortSignal): Promise<T> {
    const init: RequestInit = { method: 'GET' };
    if (signal) init.signal = signal;
    const response = await fetch(`${this.origin}${path}`, init);
    if (!response.ok) throw new ApiError(response.status, `http_${response.status}`, 'not ok');
    return (await response.json()) as T;
  }

  getState(): Promise<DaemonState> {
    return this.request<DaemonState>('/state');
  }

  listProfiles(): Promise<Profile[]> {
    return this.request<Profile[]>('/profiles');
  }

  activateProfile(name: string): Promise<DaemonState> {
    return this.request<DaemonState>(`/profiles/${encodeURIComponent(name)}/activate`, {
      method: 'POST',
      body: {},
    });
  }

  setDevToggles(toggles: Partial<{ anticache: boolean; anticomp: boolean }>): Promise<DaemonState> {
    return this.request<DaemonState>('/state', { method: 'POST', body: { dev_toggles: toggles } });
  }

  /** Redeem a pairing code. Unauthenticated — this is how the token arrives. */
  pair(code: string): Promise<{ token: string }> {
    return this.request<{ token: string }>('/pair', {
      method: 'POST',
      body: { code },
      auth: false,
    });
  }

  /**
   * Batched (request -> tab) associations.
   *
   * Best-effort: the daemon joins what it can within a bounded window and
   * ignores the rest. Nothing here can delay a flow.
   */
  submitAttribution(
    entries: unknown[],
  ): Promise<{ accepted: number; rejected: number; backfilled: number }> {
    return this.request('/attribution', { method: 'POST', body: { entries } });
  }

  getExclusions(): Promise<{ entries: { pattern: string; comment: string }[] }> {
    return this.request('/exclusions');
  }

  putExclusions(entries: { pattern: string; comment: string }[]): Promise<unknown> {
    return this.request('/exclusions', { method: 'PUT', body: { entries } });
  }
}
