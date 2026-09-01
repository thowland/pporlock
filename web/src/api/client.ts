/**
 * The control API client (SPEC-2 §4.1).
 *
 * Nothing else in the application issues fetches. Every mutating call carries
 * the bearer token and the X-Pporlock-Client header the daemon requires — the
 * latter is what makes a cross-origin form POST impossible (REQ API-013).
 */
import { resolveControlOrigin } from '../lib/control-origin';
import { filenameFromDisposition } from '../lib/download';
import type { ApiError, DaemonState, DetailLevel, FlowFilter, FlowPage } from './types';
import { filterToParams } from './types';
import type { FlowRecord, Health } from './types';
import type {
  ModuleDetail,
  ModuleSettingValue,
  ModuleStatus,
  ProfileSummary,
  ReloadResult,
  RuleIntent,
  SuggestedRule,
  ValidationResult,
} from './types';
import type { DaemonConfig, DryRunRequest, DryRunResult, SessionMeta, UnmaskResult } from './types';
import type { ExclusionEntry, Exclusions } from './types';

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

  /**
   * A module's own report, fetched with the bearer token (OI-30).
   *
   * Not a link. `request()` returns parsed JSON, and this returns text — but
   * the reason it exists at all is authentication: a plain `<a href>` is a
   * navigation, and a navigation cannot carry an Authorization header. The
   * first version of the report link was exactly that anchor and every click
   * produced `missing or invalid bearer token`.
   *
   * The obvious repair — putting the token in the URL — is forbidden outright
   * (SPEC-0 §9, and it would land in history, referrers and the audit log). So
   * the UI fetches with the header it already holds and renders the result
   * itself.
   */
  async getModuleReport(name: string): Promise<{ contentType: string; body: string }> {
    const headers: Record<string, string> = { 'X-Pporlock-Client': 'ui' };
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;

    const response = await fetch(this.url(`/modules/${encodeURIComponent(name)}/report`), {
      headers,
    });
    if (!response.ok) {
      let code = `http_${response.status}`;
      let message = response.statusText;
      try {
        const payload = (await response.json()) as ApiError;
        code = payload.error?.code ?? code;
        message = payload.error?.message ?? message;
      } catch {
        /* a non-JSON error body is still an error */
      }
      throw new ApiRequestError(response.status, code, message);
    }
    return {
      contentType: response.headers.get('content-type') ?? 'text/plain',
      body: await response.text(),
    };
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
    // Sent on every request, not only mutating ones. The daemon requires it on
    // mutations as the CSRF defence (REQ API-013), and separately requires it
    // on the *read* that unmasks a value, because unmasking is web-UI-only by
    // construction (SPEC-0 §9.3, REQ CAP-043). Sending it unconditionally means
    // no route can be reached by a client that forgot to identify itself, and
    // keeps one place responsible for saying who we are.
    headers['X-Pporlock-Client'] = 'ui';
    if (method !== 'GET' && method !== 'HEAD') {
      headers['Content-Type'] = 'application/json';
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

  /**
   * Reveal one masked value from a **live** flow (REQ CAP-043, SPEC-0 §9.3).
   *
   * Three constraints, all of which are the caller's to honour as well as the
   * daemon's: live ring buffer only, web UI only, one value per call. There is
   * deliberately no bulk form and no session equivalent — a session flow was
   * redacted before it reached the file (REQ CAP-045), so there is nothing
   * there to reveal even if a caller asked.
   *
   * The response is never cached and the revealed value is never logged.
   */
  unmask(flowId: string, fieldPath: string): Promise<UnmaskResult> {
    const params = new URLSearchParams({ unmask: fieldPath });
    return this.request<UnmaskResult>(`/flows/${encodeURIComponent(flowId)}`, { params });
  }

  clearFlows(): Promise<void> {
    return this.request<void>('/flows', { method: 'DELETE' });
  }

  /* ---------------- Exclusions (SPEC-0 §6.9) ---------------- */

  /**
   * The ClientHello exclusion list, as an envelope.
   *
   * `{entries: [...]}`, not a bare array — the daemon returns
   * `ExclusionList.to_dict()` and `contracts/openapi.yaml` says the same. Pinned
   * in `wire-shapes.test.ts`, because every other test in this suite stubs this
   * method and would agree with whatever shape the client invented.
   */
  getExclusions(): Promise<Exclusions> {
    return this.request<Exclusions>('/exclusions');
  }

  /**
   * Replace the whole list (REQ PXY-014).
   *
   * There is no append route: a caller adding one host must GET, append, and
   * PUT, or it silently deletes the other 33. `lib/exclusions.ts` owns that
   * read-modify-write so no caller has to remember it.
   *
   * `source` is sent back as it arrived, so a default entry stays labelled a
   * default after a round-trip rather than being relabelled `user` by the
   * daemon's fallback.
   */
  putExclusions(entries: ExclusionEntry[]): Promise<Exclusions> {
    return this.request<Exclusions>('/exclusions', { method: 'PUT', body: { entries } });
  }

  /* ---------------- Modules (SPEC-0 §6.6) ---------------- */

  /**
   * Modules, as a bare array.
   *
   * The contract says array (`contracts/openapi.yaml` `/modules`), and so does
   * the daemon. This client asked for `{modules: [...]}` and every test agreed,
   * because every test used a fake that returned what the client expected — so
   * the module library threw "v.modules is not iterable" the first time it met
   * a real daemon, and nothing before that had a chance to notice.
   */
  listModules(): Promise<ModuleStatus[]> {
    return this.request<ModuleStatus[]>('/modules');
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

  /**
   * `PATCH` carries `enabled`, `priority` and declared settings — never file
   * content.
   *
   * `config` replaces the module's overrides wholesale, which is how "reset
   * this field to its default" is expressed: omit the key. An undeclared key
   * or a value of the wrong type is a 400 and nothing is written, so a form
   * with one bad field never half applies.
   */
  patchModule(
    name: string,
    changes: { enabled?: boolean; priority?: number; config?: Record<string, ModuleSettingValue> },
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

  /**
   * Profiles, as a bare array. Same correction as listModules.
   *
   * There is no `active` field here and there never was: which profile is
   * active is daemon state, and `GET /state` is where it lives.
   */
  listProfiles(): Promise<ProfileSummary[]> {
    return this.request<ProfileSummary[]>('/profiles');
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

  /* ---------------- Sessions and dry run (SPEC-0 §6.8) ---------------- */

  listSessions(): Promise<SessionMeta[]> {
    return this.request<SessionMeta[]>('/sessions');
  }

  getSession(sessionId: string): Promise<SessionMeta> {
    return this.request<SessionMeta>(`/sessions/${encodeURIComponent(sessionId)}`);
  }

  /** Recording is opt-in and off by default (REQ CAP-020). */
  startRecording(name: string): Promise<SessionMeta> {
    return this.request<SessionMeta>('/sessions', { method: 'POST', body: { name } });
  }

  stopRecording(sessionId: string): Promise<SessionMeta> {
    return this.request<SessionMeta>(`/sessions/${encodeURIComponent(sessionId)}/stop`, {
      method: 'POST',
      body: {},
    });
  }

  renameSession(sessionId: string, name: string): Promise<SessionMeta> {
    return this.request<SessionMeta>(`/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'PATCH',
      body: { name },
    });
  }

  deleteSession(sessionId: string): Promise<void> {
    return this.request<void>(`/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
  }

  /**
   * Recorded flows, same filter vocabulary as the live ring (SPEC-0 §6.5).
   *
   * Same shape as `listFlows`, deliberately: the session browser reuses the
   * live table and detail components, and a divergent page shape here would be
   * the first crack in that (SPEC-2 §8.2).
   */
  listSessionFlows(
    sessionId: string,
    filter: FlowFilter = {},
    opts: PageOpts = {},
  ): Promise<FlowPage> {
    const params = filterToParams(filter);
    if (opts.limit !== undefined) params.set('limit', String(opts.limit));
    if (opts.cursor) params.set('cursor', opts.cursor);
    if (opts.detail) params.set('detail', opts.detail);
    return this.request<FlowPage>(`/sessions/${encodeURIComponent(sessionId)}/flows`, { params });
  }

  /** Where an export lives. Not linkable — see `fetchSessionExport`. */
  sessionExportUrl(sessionId: string, format: 'har' | 'pporlock'): string {
    return this.url(
      `/sessions/${encodeURIComponent(sessionId)}/export`,
      new URLSearchParams({ format }),
    );
  }

  /**
   * Fetch a session export with the bearer token (REQ CAP-024, OI-35).
   *
   * This was an `<a href download>` for the whole life of the project, and it
   * could never have worked: a navigation carries no Authorization header, so
   * the daemon answered 401 and Chrome rendered that as "file was not available
   * on the site" — a message that names neither the cause nor a remedy. The
   * same anchor made the same mistake for the module report link (OI-30); this
   * is that repair, applied to the surface that still had it.
   *
   * Putting the token in the query string would "fix" it and is forbidden
   * (SPEC-0 §9): the URL reaches history, referrers and the audit log.
   */
  async fetchSessionExport(
    sessionId: string,
    format: 'har' | 'pporlock',
  ): Promise<{ blob: Blob; filename: string }> {
    const headers: Record<string, string> = { 'X-Pporlock-Client': 'ui' };
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;

    const response = await fetch(this.sessionExportUrl(sessionId, format), { headers });
    if (!response.ok) {
      let code = `http_${response.status}`;
      let message = response.statusText;
      try {
        const payload = (await response.json()) as ApiError;
        code = payload.error?.code ?? code;
        message = payload.error?.message ?? message;
      } catch {
        /* a non-JSON error body is still an error */
      }
      throw new ApiRequestError(response.status, code, message);
    }
    return {
      blob: await response.blob(),
      filename:
        filenameFromDisposition(response.headers.get('content-disposition')) ??
        `${sessionId}.${format}.json`,
    };
  }

  /**
   * Evaluate candidate modules against a recorded session (REQ CAP-030).
   *
   * This **executes the candidate module's Python hooks** (REQ CAP-032). It
   * touches no live traffic, but "no live traffic" is not "no code ran", and
   * every surface that offers this must say so.
   */
  dryRun(sessionId: string, request: DryRunRequest): Promise<DryRunResult> {
    return this.request<DryRunResult>(`/sessions/${encodeURIComponent(sessionId)}/dryrun`, {
      method: 'POST',
      body: request,
    });
  }

  /* ---------------- Configuration (SPEC-0 §6.9) ---------------- */

  /** The *effective* configuration, defaults included (REQ CAP-044). */
  getConfig(): Promise<DaemonConfig> {
    return this.request<DaemonConfig>('/config');
  }

  /**
   * Change configuration sections. The body is a partial: sections omitted are
   * left alone, which is why the settings UI sends only what it edits rather
   * than echoing the whole document back.
   */
  putConfig(sections: Record<string, unknown>): Promise<DaemonConfig> {
    return this.request<DaemonConfig>('/config', { method: 'PUT', body: sections });
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
