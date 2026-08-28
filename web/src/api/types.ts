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
  /**
   * Optional, and it is not merely theoretical: `contracts/openapi.yaml` does
   * not require it and the daemon does not yet send it (per-module cost is
   * REQ PRF-007). Reading through it unconditionally crashed the module
   * library against a real daemon while every test passed.
   */
  stats?: ModuleStats | undefined;
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

/* ------------------------------------------------------------------ *
 * Sessions and dry run (SPEC-0 §6.8)                                  *
 * ------------------------------------------------------------------ */

/** `GET /sessions`. Mirrors the daemon's `SessionMeta.to_dict`. */
export interface SessionMeta {
  session_id: string;
  name: string;
  state: 'recording' | 'stopped';
  started_at: string;
  stopped_at: string | null;
  flow_count: number;
  size_bytes: number;
  profile: string;
  /**
   * Flows the writer dropped under overflow (REQ CAP-023). Surfaced in the
   * list because a session with drops is not a faithful recording, and a dry
   * run against it is answering a slightly different question.
   */
  dropped: number;
  schema_version?: number;
}

/** `POST /sessions/{id}/dryrun` request body (SPEC-0 §6.8). */
export interface DryRunRequest {
  modules?: { name: string; files: Record<string, string> }[];
  use_installed?: string[];
  profile?: string | null;
  limit?: number;
  include_diffs?: boolean;
}

/** REQ CAP-033. Every field is an aggregate over the evaluated flows. */
export interface DryRunSummary {
  flows_evaluated: number;
  matched: number;
  modified: number;
  blocked: number;
  errors: number;
  avg_ms: number;
  p95_ms: number;
}

/** `op` is one of add | remove | replace (contracts/openapi.yaml DryRunResult). */
export interface DryRunHeaderDiff {
  op: string;
  name: string;
  value?: string | null;
}

export interface DryRunBodyDiff {
  kind: string;
  text: string;
  truncated: boolean;
}

export interface DryRunDiff {
  headers?: DryRunHeaderDiff[];
  body?: DryRunBodyDiff | null;
}

export interface DryRunFlowResult {
  flow_id: string;
  url: string;
  provenance?: import('@contracts/types').Provenance | undefined;
  diff?: DryRunDiff | undefined;
}

export interface DryRunResult {
  summary: DryRunSummary;
  results: DryRunFlowResult[];
  /** Present when the daemon capped the per-flow list (REQ MCP-005). */
  results_total?: number;
  results_shown?: number;
  results_note?: string;
}

/** `GET /flows/{id}?unmask=<field_path>` (REQ CAP-043). */
export interface UnmaskResult {
  flow_id: string;
  field_path: string;
  value: string;
}

/* ------------------------------------------------------------------ *
 * Configuration (SPEC-0 §6.9, §9.2 — REQ CAP-044)                     *
 * ------------------------------------------------------------------ */

/**
 * The redaction section of `GET /config`. This is the *effective*
 * configuration — what is in force right now, defaults included — which is the
 * whole point of the route: "redaction is configurable" is otherwise a claim
 * nobody can check (REQ CAP-044).
 */
export interface RedactionConfig {
  enabled: boolean;
  header_patterns: string[];
  json_key_patterns: string[];
}

/**
 * `GET /config` returns every section. Only the sections the UI edits are
 * described; the rest travel as opaque values so a daemon that grows a section
 * does not break the round-trip.
 */
export interface DaemonConfig {
  redaction: RedactionConfig;
  [section: string]: unknown;
}
