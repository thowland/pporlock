/**
 * Provenance rendering shared with the web UI (SPEC-3 §7.2).
 *
 * Deliberately duplicated rather than imported: the extension and the web UI
 * are separately built and have no shared package, and a panel that silently
 * disagreed with the web UI about what an outcome means would be worse than one
 * that repeats a table. The completeness tests on both sides are what keep them
 * honest — each iterates the full enum from the contract.
 */

export const PHASE_ORDER = [
  'clienthello',
  'request_short_circuit',
  'request_headers',
  'buffering_decision',
  'response_headers',
  'response_body',
  'websocket',
] as const;

export const PHASE_LABEL: Record<string, string> = {
  clienthello: 'TLS ClientHello',
  request_short_circuit: 'Request — short-circuit',
  request_headers: 'Request — headers',
  buffering_decision: 'Buffering decision',
  response_headers: 'Response — headers',
  response_body: 'Response — body',
  websocket: 'WebSocket',
};

export const OUTCOME_LABEL: Record<string, string> = {
  applied: 'applied',
  no_change: 'no change',
  skipped_streamed: 'skipped — response streamed',
  skipped_budget: 'skipped — time budget exhausted',
  skipped_short_circuit: 'skipped — an earlier rule short-circuited',
  skipped_disabled: 'skipped — module disabled',
  error: 'error',
};

/** Outcomes meaning something did not happen. Never styled as success. */
export const NEGATIVE_OUTCOMES = new Set([
  'skipped_streamed',
  'skipped_budget',
  'skipped_short_circuit',
  'skipped_disabled',
  'error',
]);

export const NOTE_LABEL: Record<string, string> = {
  response_streamed: 'Response was streamed, so body transforms could not run',
  transform_budget_exceeded: 'The per-flow time budget ran out; a transform was cut',
  module_quarantined: 'A module was disabled after repeated failures',
  map_local_missing: 'A map_local rule pointed at a file that is not there',
  csp_modified: 'Content-Security-Policy was changed or removed',
  sri_stripped: 'Subresource-integrity attributes were removed',
  script_injected: 'A script was injected into this document',
  dev_toggle_active: 'A development toggle was active for this flow',
  body_truncated: 'The body was larger than the capture cap and was cut',
  module_error: 'A module raised while handling this flow',
  passthrough_excluded: 'The connection was tunnelled undecrypted',
  attribution_missing: 'No browser tab could be associated with this flow',
  module_deprecation: 'A module used something scheduled for removal',
};

export const SEVERITY_RANK: Record<string, number> = { error: 0, warning: 1, info: 2 };
