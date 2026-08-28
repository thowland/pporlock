/**
 * Wire types.
 *
 * Re-exported from the generated contract types so application code imports
 * from one place. Nothing here may describe a wire shape by hand — add it to
 * contracts/schemas/ and regenerate (SPEC-0 §1.1).
 */
export type {
  Event as WireEvent,
  EventType,
  FlowPassthrough,
  FlowRecord,
  FlowRequest,
  FlowResponse,
  FlowTiming,
  FlowWebsocket,
  HeaderPairs,
  Health,
  NoteCode,
  Outcome,
  Phase,
  Provenance,
  ProvenanceEntry,
  ProvenanceNote,
  Severity,
} from '@contracts/types';

/** Representation level (SPEC-0 §6.3). Bodies dominate response size. */
export type DetailLevel = 'summary' | 'full' | 'bodies';

/** The SPEC-0 §6.5 filter vocabulary, identical to the daemon's. */
export interface FlowFilter {
  host?: string;
  path?: string;
  method?: string;
  status?: string;
  content_type?: string;
  dest?: string;
  tab_id?: number;
  modified?: boolean;
  blocked?: boolean;
  module?: string;
  note_code?: string;
  since?: string;
  until?: string;
  q?: string;
}

export interface FlowPage {
  flows: import('@contracts/types').FlowRecord[];
  next_cursor: string | null;
  total_estimate: number;
}

export interface DaemonState {
  version: string;
  mitmproxy_version: string;
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
  clients: { mcp_connected: number; mcp_read_only: boolean };
}

export interface ApiError {
  error: { code: string; message: string; detail?: Record<string, unknown>; trace?: string | null };
}

/** Turn a filter into query parameters, omitting anything unset. */
export function filterToParams(filter: FlowFilter): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filter)) {
    if (value === undefined || value === null || value === '') continue;
    params.set(key, String(value));
  }
  return params;
}

/* ------------------------------------------------------------------ *
 * Modules and profiles (SPEC-0 §6.6, §6.7)                            *
 * ------------------------------------------------------------------ */

/**
 * Manifest, rule and profile shapes come from the schemas — never restate them
 * here. What follows is only the API *envelope* around them, which SPEC-0
 * describes in prose rather than in `contracts/schemas/`.
 */
export type { Match, ModuleManifest, Profile, Rule } from '@contracts/types';

/** SPEC-0 §6.6 module status `state`. */
export type ModuleState = 'loaded' | 'disabled' | 'quarantined' | 'load_error';

/** Present only when `state === 'load_error'`. `line` places an editor marker. */
export interface ModuleLoadError {
  code: string;
  message: string;
  trace?: string | null;
  line?: number | null;
}

/** Present only when `state === 'quarantined'` (REQ MOD-025). */
export interface ModuleQuarantine {
  reason: string;
  failures: number;
  since: string;
}

export interface ModuleStats {
  flows_matched: number;
  flows_modified: number;
  errors: number;
  avg_ms: number;
}

export interface ModuleStatus {
  name: string;
  version: string;
  enabled: boolean;
  priority: number;
  state: ModuleState;
  has_python: boolean;
  rule_count: number;
  error: ModuleLoadError | null;
  quarantine: ModuleQuarantine | null;
  stats: ModuleStats;
}

/**
 * `GET /modules/{name}`. Files are keyed by their on-disk name so the editor
 * never has to know how many there are.
 */
export interface ModuleDetail extends ModuleStatus {
  files: Record<string, string>;
  assets?: string[];
}

export const MODULE_YAML = 'module.yaml';
export const MODULE_PY = 'module.py';

/** Read a module file, tolerating a daemon that omits an absent optional file. */
export function moduleFile(detail: Pick<ModuleDetail, 'files'>, name: string): string {
  return Object.prototype.hasOwnProperty.call(detail.files, name)
    ? // eslint-disable-next-line security/detect-object-injection
      (detail.files[name] ?? '')
    : '';
}

/**
 * One `POST /validate` finding (REQ API-027). `line`/`column` are 1-based when
 * the daemon can place the error; absent when it cannot.
 */
export interface ValidationIssue {
  code: string;
  message: string;
  file?: string | null;
  line?: number | null;
  column?: number | null;
  severity?: 'error' | 'warning';
}

export interface ValidationResult {
  ok: boolean;
  errors: ValidationIssue[];
  warnings?: ValidationIssue[];
}

/** `POST /modules/reload` (REQ MOD-004). */
export interface ReloadResult {
  loaded: number;
  enabled: number;
  quarantined: number;
  errors: ModuleLoadError[];
}

export type ProfileSummary = import('@contracts/types').Profile & { active?: boolean };

export interface ProfileList {
  profiles: ProfileSummary[];
  active: string;
}

/** The four intents SPEC-2 §7.4 offers from a flow. */
export type RuleIntent = 'block' | 'map_local' | 'redirect' | 'headers';

/** `POST /flows/{id}/suggest-rule` (REQ WUI-008, MCP-014). */
export interface SuggestedRule {
  rule: import('@contracts/types').Rule;
  module?: string | null;
}
