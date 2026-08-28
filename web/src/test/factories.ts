/** Test factories for wire shapes. Shared, not duplicated per test file. */
import type { DryRunResult, FlowRecord, Provenance, SessionMeta } from '../api/types';

export function makeProvenance(overrides: Partial<Provenance> = {}): Provenance {
  return {
    profile: 'default',
    evaluated_modules: [],
    entries: [],
    notes: [],
    total_ms: 0,
    short_circuited_by: null,
    ...overrides,
  } as Provenance;
}

/**
 * A flow. Overrides accept explicit `undefined` so a test can express "this
 * flow has no request" — a passthrough, for instance — which
 * exactOptionalPropertyTypes would otherwise forbid.
 */
export function makeFlow(
  overrides: { [K in keyof FlowRecord]?: FlowRecord[K] | undefined } = {},
): FlowRecord {
  return {
    flow_id: 'f0',
    kind: 'http',
    started_at: '2026-08-27T14:03:22.417Z',
    completed_at: '2026-08-27T14:03:22.694Z',
    tab_id: null,
    modified: false,
    blocked: false,
    redacted: false,
    request: {
      method: 'GET',
      scheme: 'https',
      host: 'cdn.example.com',
      port: 443,
      path: '/a/analytics.js',
      query: [],
      url: 'https://cdn.example.com/a/analytics.js',
      http_version: 'HTTP/2',
      dest: 'script',
      headers: [],
      body_size: 1234,
      body_truncated: false,
    },
    response: {
      status: 200,
      reason: 'OK',
      http_version: 'HTTP/2',
      headers: [],
      content_type: 'application/javascript',
      body_size: 4821,
      body_truncated: false,
      streamed: false,
    },
    timing: {
      dns_ms: null,
      connect_ms: null,
      request_ms: null,
      upstream_ms: null,
      response_ms: null,
      pporlock_ms: 1.5,
      total_ms: null,
    },
    provenance: makeProvenance(),
    ...overrides,
  } as FlowRecord;
}

/** A recorded session (SPEC-0 §6.8). */
export function makeSession(overrides: Partial<SessionMeta> = {}): SessionMeta {
  return {
    session_id: 's1',
    name: 'checkout-bug',
    state: 'stopped',
    started_at: '2026-08-27T14:00:00.000Z',
    stopped_at: '2026-08-27T14:05:00.000Z',
    flow_count: 42,
    size_bytes: 204800,
    profile: 'default',
    dropped: 0,
    ...overrides,
  };
}

/** A dry-run response (SPEC-0 §6.8, REQ CAP-033). */
export function makeDryRun(overrides: Partial<DryRunResult> = {}): DryRunResult {
  return {
    summary: {
      flows_evaluated: 500,
      matched: 63,
      modified: 41,
      blocked: 22,
      errors: 0,
      avg_ms: 0.9,
      p95_ms: 3.1,
    },
    results: [],
    ...overrides,
  };
}
