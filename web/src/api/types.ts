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
