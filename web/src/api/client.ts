/**
 * The control API client (SPEC-2 §4.1).
 *
 * Nothing else in the application issues fetches. Every mutating call carries
 * the bearer token and the X-Pporlock-Client header the daemon requires — the
 * latter is what makes a cross-origin form POST impossible (REQ API-013).
 */
import { resolveControlOrigin } from '../lib/control-origin';
import type { ApiError, DaemonState, DetailLevel, FlowFilter, FlowPage } from './types';
import { filterToParams } from './types';
import type { FlowRecord, Health } from './types';
import type {
  ModuleDetail,
  ModuleStatus,
  ProfileList,
  ProfileSummary,
  ReloadResult,
  RuleIntent,
  SuggestedRule,
  ValidationResult,
} from './types';

export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'ApiRequestError';
  }
}

export interface PageOpts {
  limit?: number;
  cursor?: string | null;
  detail?: DetailLevel;
}

export class ApiClient {
  readonly origin: string;
  private token: string | null = null;

  constructor(origin?: string) {
    this.origin = resolveControlOrigin(origin);
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  get hasToken(): boolean {
    return this.token !== null && this.token !== '';
  }

  private url(path: string, params?: URLSearchParams): string {
    const query = params && [...params.keys()].length > 0 ? `?${params.toString()}` : '';
    return `${this.origin}${path}${query}`;
  }

  private async request<T>(
    path: string,
    {
      params,
      method = 'GET',
      body,
    }: { params?: URLSearchParams; method?: string; body?: unknown } = {},
  ): Promise<T> {
    const headers: Record<string, string> = {};
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
    if (method !== 'GET' && method !== 'HEAD') {
      headers['Content-Type'] = 'application/json';
      headers['X-Pporlock-Client'] = 'ui';
    }

    const init: RequestInit = { method, headers };
    if (body !== undefined) init.body = JSON.stringify(body);
    const response = await fetch(this.url(path, params), init);

    if (response.status === 204) return undefined as T;

    if (!response.ok) {
      let code = `http_${response.status}`;
      let message = response.statusText;
      try {
        const payload = (await response.json()) as ApiError;
        code = payload.error?.code ?? code;
        message = payload.error?.message ?? message;
      } catch {
        // A non-JSON error body is still an error; keep the status text.
      }
      throw new ApiRequestError(response.status, code, message);
    }

    return (await response.json()) as T;
  }

  /** The only unauthenticated route. Used to tell "daemon down" from "not paired". */
  health(): Promise<Health> {
    return this.request<Health>('/state/health');
  }

  getState(): Promise<DaemonState> {
    return this.request<DaemonState>('/state');
  }

  setDevToggles(toggles: Partial<{ anticache: boolean; anticomp: boolean }>): Promise<DaemonState> {
    return this.request<DaemonState>('/state', { method: 'POST', body: { dev_toggles: toggles } });
  }

  listFlows(filter: FlowFilter = {}, opts: PageOpts = {}): Promise<FlowPage> {
    const params = filterToParams(filter);
    if (opts.limit !== undefined) params.set('limit', String(opts.limit));
    if (opts.cursor) params.set('cursor', opts.cursor);
    if (opts.detail) params.set('detail', opts.detail);
    return this.request<FlowPage>('/flows', { params });
  }

  getFlow(flowId: string, detail: DetailLevel = 'full'): Promise<FlowRecord> {
    const params = new URLSearchParams({ detail });
    return this.request<FlowRecord>(`/flows/${encodeURIComponent(flowId)}`, { params });
  }

  clearFlows(): Promise<void> {
    return this.request<void>('/flows', { method: 'DELETE' });
  }

  getExclusions(): Promise<{ entries: { pattern: string; comment: string; source: string }[] }> {
    return this.request('/exclusions');
  }

  /* ---------------- Modules (SPEC-0 §6.6) ---------------- */

  listModules(): Promise<{ modules: ModuleStatus[] }> {
    return this.request<{ modules: ModuleStatus[] }>('/modules');
  }

  getModule(name: string): Promise<ModuleDetail> {
    return this.request<ModuleDetail>(`/modules/${encodeURIComponent(name)}`);
  }

  /**
   * Create never enables (REQ MCP-030) — the daemon enforces it and the UI
   * mirrors it, so enabling is always a separate, deliberate PATCH.
   */
  createModule(name: string, files: Record<string, string>): Promise<ModuleStatus> {
    return this.request<ModuleStatus>('/modules', { method: 'POST', body: { name, files } });
  }

  replaceModule(name: string, files: Record<string, string>): Promise<ModuleStatus> {
    return this.request<ModuleStatus>(`/modules/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: { files },
    });
  }

  /** `PATCH` carries `enabled` and `priority` only — never file content. */
  patchModule(
    name: string,
    changes: { enabled?: boolean; priority?: number },
  ): Promise<ModuleStatus> {
    return this.request<ModuleStatus>(`/modules/${encodeURIComponent(name)}`, {
      method: 'PATCH',
      body: changes,
    });
  }

  deleteModule(name: string): Promise<void> {
    return this.request<void>(`/modules/${encodeURIComponent(name)}`, { method: 'DELETE' });
  }

  reloadModules(): Promise<ReloadResult> {
    return this.request<ReloadResult>('/modules/reload', { method: 'POST', body: {} });
  }

  /** Validates a candidate module and installs nothing (REQ API-027). */
  validateModule(files: Record<string, string>): Promise<ValidationResult> {
    return this.request<ValidationResult>('/validate', { method: 'POST', body: { files } });
  }

  /* ---------------- Profiles (SPEC-0 §6.7) ---------------- */

  listProfiles(): Promise<ProfileList> {
    return this.request<ProfileList>('/profiles');
  }

  createProfile(profile: ProfileSummary): Promise<ProfileSummary> {
    return this.request<ProfileSummary>('/profiles', { method: 'POST', body: profile });
  }

  replaceProfile(name: string, profile: ProfileSummary): Promise<ProfileSummary> {
    return this.request<ProfileSummary>(`/profiles/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: profile,
    });
  }

  deleteProfile(name: string): Promise<void> {
    return this.request<void>(`/profiles/${encodeURIComponent(name)}`, { method: 'DELETE' });
  }

  activateProfile(name: string): Promise<{ active: string }> {
    return this.request<{ active: string }>(`/profiles/${encodeURIComponent(name)}/activate`, {
      method: 'POST',
      body: {},
    });
  }

  /** Candidate rule derived from a flow (REQ WUI-008, MCP-014). */
  suggestRule(flowId: string, intent: RuleIntent): Promise<SuggestedRule> {
    return this.request<SuggestedRule>(`/flows/${encodeURIComponent(flowId)}/suggest-rule`, {
      method: 'POST',
      body: { intent },
    });
  }

  /**
   * The SSE endpoint URL. Deliberately carries no token.
   *
   * EventSource cannot set headers, which is why token-in-query-string is the
   * usual shortcut here. We do not take it: a URL lands in logs, in history,
   * and in Referer, and this token grants read access to captured traffic
   * (implementation-plan.md §2.5). The event stream uses streaming fetch with
   * an Authorization header instead — see EventStream.
   */
  eventsUrl(filter: FlowFilter = {}, kinds?: string[]): string {
    const params = filterToParams(filter);
    if (kinds && kinds.length > 0) params.set('kinds', kinds.join(','));
    return this.url('/events', params);
  }

  /** Headers for the event stream. Kept here so the token has one owner. */
  streamHeaders(lastEventId?: string | null): Record<string, string> {
    const headers: Record<string, string> = { Accept: 'text/event-stream' };
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
    if (lastEventId) headers['Last-Event-ID'] = lastEventId;
    return headers;
  }
}
