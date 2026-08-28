/**
 * The provenance view (SPEC-2 §6.3, REQ CAP-013, DOC-003).
 *
 * The most important screen in the application.
 *
 * Silent breakage is the characteristic failure of this class of tool: the
 * proxy considers a flow successful, the page is subtly wrong, and the cause is
 * three rules deep. This is where that becomes visible.
 *
 * Two rules govern the presentation, and both run against the usual instinct to
 * show successes prominently:
 *
 *  - Non-`applied` outcomes are as prominent as applied ones. The whole point
 *    is explaining why something *didn't* happen.
 *  - `warning` and `error` notes are never collapsed, and they appear above the
 *    timeline, because they describe conditions no rule asked for.
 */
import type { Provenance, ProvenanceEntry, ProvenanceNote } from '../../api/types';

/** Phases in pipeline order (SPEC-0 §4.2). */
const PHASE_ORDER = [
  'clienthello',
  'request_short_circuit',
  'request_headers',
  'buffering_decision',
  'response_headers',
  'response_body',
  'websocket',
] as const;

const PHASE_LABEL: Record<string, string> = {
  clienthello: 'TLS ClientHello',
  request_short_circuit: 'Request — short-circuit',
  request_headers: 'Request — headers',
  buffering_decision: 'Buffering decision',
  response_headers: 'Response — headers',
  response_body: 'Response — body',
  websocket: 'WebSocket',
};

/**
 * What each outcome means, in the words a user needs.
 *
 * Every outcome in SPEC-0 §4.3 has an entry. A missing one would render as a
 * bare enum value, which is precisely the silence this view exists to remove.
 */
const OUTCOME_LABEL: Record<string, string> = {
  applied: 'applied',
  no_change: 'no change',
  skipped_streamed: 'skipped — response streamed',
  skipped_budget: 'skipped — time budget exhausted',
  skipped_short_circuit: 'skipped — an earlier rule short-circuited',
  skipped_disabled: 'skipped — module disabled',
  error: 'error',
};

/** Outcomes that mean something did not happen, and must not read as success. */
const NEGATIVE_OUTCOMES = new Set([
  'skipped_streamed',
  'skipped_budget',
  'skipped_short_circuit',
  'skipped_disabled',
  'error',
]);

/**
 * What each note code means. Every code in SPEC-0 §4.4 has an entry; a test
 * iterates the enum to keep it that way.
 */
const NOTE_LABEL: Record<string, string> = {
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

function Notes({ notes }: { notes: ProvenanceNote[] }) {
  if (notes.length === 0) return null;
  return (
    <div className="prov-notes">
      {notes.map((note, index) => (
        <div key={`${note.code}-${index}`} className={`prov-note sev-${note.severity}`}>
          <div className="prov-note-head">
            <span className="prov-note-code">{note.code}</span>
            {note.module && <span className="prov-note-module">{note.module}</span>}
          </div>
          <div className="prov-note-meaning">{NOTE_LABEL[note.code] ?? note.code}</div>
          {note.message && <div className="prov-note-message">{note.message}</div>}
          {note.detail && Object.keys(note.detail).length > 0 && <Detail detail={note.detail} />}
        </div>
      ))}
    </div>
  );
}

function Detail({ detail }: { detail: Record<string, unknown> }) {
  const entries = Object.entries(detail).filter(([, v]) => v !== null && v !== undefined);
  if (entries.length === 0) return null;
  return (
    <dl className="prov-detail">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Entry({
  entry,
  shortCircuitedBy,
  onOpenModule,
}: {
  entry: ProvenanceEntry;
  shortCircuitedBy: string | null | undefined;
  onOpenModule?: ((module: string) => void) | undefined;
}) {
  const negative = NEGATIVE_OUTCOMES.has(entry.outcome);
  const isCulprit =
    shortCircuitedBy !== null &&
    shortCircuitedBy !== undefined &&
    entry.rule_id === shortCircuitedBy;

  return (
    <div className={`prov-entry outcome-${entry.outcome} ${negative ? 'negative' : ''}`}>
      <div className="prov-entry-head">
        <span className="prov-rule">
          {entry.module ? (
            <button
              type="button"
              className="linkish"
              title={`Open ${entry.module}`}
              onClick={() => onOpenModule?.(entry.module)}
            >
              {entry.module}
            </button>
          ) : (
            <span className="faint">engine</span>
          )}
          {entry.rule_name && <span className="prov-rule-name">{entry.rule_name}</span>}
          {entry.rule_id && <span className="prov-rule-id">{entry.rule_id}</span>}
        </span>
        <span className="spacer" />
        <span className="prov-action">{entry.action}</span>
        <span className={`prov-outcome sev-${negative ? 'warning' : 'ok'}`}>
          {OUTCOME_LABEL[entry.outcome] ?? entry.outcome}
        </span>
        <span className="prov-ms faint">{entry.duration_ms.toFixed(2)}ms</span>
      </div>

      {isCulprit && (
        // "An earlier rule ate it" is the single most common confusion when
        // debugging a rule set, so it is called out rather than inferred.
        <div className="prov-culprit">
          This rule short-circuited the flow — nothing after it in this class ran.
        </div>
      )}

      {entry.detail && Object.keys(entry.detail).length > 0 && (
        <Detail detail={entry.detail as Record<string, unknown>} />
      )}
    </div>
  );
}

export function ProvenanceView({
  provenance,
  onOpenModule,
}: {
  provenance: Provenance | undefined;
  onOpenModule?: ((module: string) => void) | undefined;
}) {
  if (!provenance) {
    return <div className="empty-small">No provenance recorded for this flow.</div>;
  }

  const notes = (provenance.notes ?? []) as ProvenanceNote[];
  const entries = (provenance.entries ?? []) as ProvenanceEntry[];

  // Errors first, then warnings, then info: the ordering a reader needs.
  // A Map rather than a record: severity comes from the wire, and
  // security/detect-object-injection is right that indexing an object with it
  // is a pattern worth avoiding even when the values are a closed enum.
  const severityRank = new Map([
    ['error', 0],
    ['warning', 1],
    ['info', 2],
  ]);
  const orderedNotes = [...notes].sort(
    (a, b) => (severityRank.get(a.severity) ?? 3) - (severityRank.get(b.severity) ?? 3),
  );

  const byPhase = PHASE_ORDER.map((phase) => ({
    phase,
    entries: entries.filter((e) => e.phase === phase),
  })).filter((group) => group.entries.length > 0);

  return (
    <div className="provenance">
      <div className="prov-summary">
        <span>
          profile <b>{provenance.profile}</b>
        </span>
        <span>
          total <b>{provenance.total_ms.toFixed(2)}ms</b>
        </span>
        {provenance.evaluated_modules && provenance.evaluated_modules.length > 0 && (
          <span>
            modules <b>{provenance.evaluated_modules.join(', ')}</b>
          </span>
        )}
      </div>

      <Notes notes={orderedNotes} />

      {byPhase.length === 0 ? (
        <div className="empty-small">No rule matched this flow. It passed through unchanged.</div>
      ) : (
        byPhase.map(({ phase, entries: phaseEntries }) => (
          <section key={phase} className="prov-phase">
            {/* phase comes from PHASE_ORDER, a module constant, not from the
                wire — the sink the rule warns about does not exist here. */}
            {/* eslint-disable-next-line security/detect-object-injection */}
            <h4>{PHASE_LABEL[phase] ?? phase}</h4>
            {phaseEntries.map((entry) => (
              <Entry
                key={`${entry.seq}-${entry.rule_id}`}
                entry={entry}
                shortCircuitedBy={provenance.short_circuited_by}
                onOpenModule={onOpenModule}
              />
            ))}
          </section>
        ))
      )}
    </div>
  );
}

export { NOTE_LABEL, OUTCOME_LABEL, PHASE_LABEL, NEGATIVE_OUTCOMES };
