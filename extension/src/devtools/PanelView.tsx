/**
 * The DevTools panel (SPEC-3 §7, REQ EXT-013).
 *
 * The designated primary debugging affordance, and not optional. Scoped to the
 * inspected tab, so it answers "what did pporlock do to *this page*" — the
 * question you actually have when a page is subtly wrong.
 *
 * It holds no authoritative state: it refetches rather than assuming its buffer
 * is complete (SPEC-3 §7.4).
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ControlApi } from '../shared/api';
import type { FlowRecord, ProvenanceEntry, ProvenanceNote } from '../shared/flows';
import { describeMasked, isMasked } from '../shared/redaction';
import {
  NEGATIVE_OUTCOMES,
  NOTE_LABEL,
  OUTCOME_LABEL,
  PHASE_LABEL,
  PHASE_ORDER,
  SEVERITY_RANK,
} from './provenance';

type Chip = 'all' | 'modified' | 'blocked' | 'warnings' | 'unattributed';

export function severityOf(flow: FlowRecord): 'error' | 'warning' | null {
  const notes = flow.provenance?.notes ?? [];
  if (notes.some((n: ProvenanceNote) => n.severity === 'error')) return 'error';
  if (notes.some((n: ProvenanceNote) => n.severity === 'warning')) return 'warning';
  return null;
}

/**
 * Headers, with masked values rendered as what they are (REQ CAP-043).
 *
 * There is deliberately no reveal control. Unmasking is live-ring-only and web
 * UI-only (SPEC-0 §9.3); a second path here would be a second thing to get
 * wrong. What the panel does give you is the fingerprint, so you can tell
 * whether two requests carried the same cookie without seeing it.
 */
function Headers({ title, headers }: { title: string; headers: [string, string][] | undefined }) {
  if (!headers || headers.length === 0) return null;
  return (
    <section className="headers">
      <h4>{title}</h4>
      <dl>
        {headers.map(([name, value], i) => (
          <div key={`${name}-${i}`} className={isMasked(value) ? 'masked' : undefined}>
            <dt>{name}</dt>
            <dd
              title={isMasked(value) ? 'Redacted — reveal is available in the web UI only' : value}
            >
              {describeMasked(value)}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function Provenance({ flow }: { flow: FlowRecord }) {
  const provenance = flow.provenance;
  if (!provenance) return <p className="dim">No provenance recorded.</p>;

  const notes = [...(provenance.notes ?? [])].sort(
    (a: ProvenanceNote, b: ProvenanceNote) =>
      (SEVERITY_RANK[a.severity] ?? 3) - (SEVERITY_RANK[b.severity] ?? 3),
  );
  const entries: ProvenanceEntry[] = provenance.entries ?? [];

  return (
    <div className="prov">
      {notes.length > 0 && (
        <div className="notes">
          {notes.map((note: ProvenanceNote, i: number) => (
            <div key={`${note.code}-${i}`} className={`note sev-${note.severity}`}>
              <b>{note.code}</b>
              <span>{NOTE_LABEL[note.code] ?? note.code}</span>
              {note.module && <em>{note.module}</em>}
            </div>
          ))}
        </div>
      )}

      {entries.length === 0 ? (
        <p className="dim">No rule matched this flow. It passed through unchanged.</p>
      ) : (
        PHASE_ORDER.map((phase) => {
          const inPhase = entries.filter((e) => e.phase === phase);
          if (inPhase.length === 0) return null;
          return (
            <section key={phase}>
              {/* phase comes from PHASE_ORDER, a module constant, not the wire. */}
              {/* eslint-disable-next-line security/detect-object-injection */}
              <h4>{PHASE_LABEL[phase]}</h4>
              {inPhase.map((entry) => {
                const negative = NEGATIVE_OUTCOMES.has(entry.outcome);
                return (
                  <div
                    key={`${entry.seq}-${entry.rule_id}`}
                    className={`entry ${negative ? 'negative' : ''}`}
                  >
                    <span className="rule">{entry.rule_name ?? entry.rule_id ?? 'engine'}</span>
                    <span className="action">{entry.action}</span>
                    <span className={`outcome ${negative ? 'bad' : 'ok'}`}>
                      {OUTCOME_LABEL[entry.outcome] ?? entry.outcome}
                    </span>
                    {provenance.short_circuited_by === entry.rule_id && (
                      <div className="culprit">
                        This rule short-circuited the flow — nothing after it ran.
                      </div>
                    )}
                  </div>
                );
              })}
            </section>
          );
        })
      )}
    </div>
  );
}

export function PanelView({
  api,
  tabId,
  onOpenModule,
  pollMs = 2000,
}: {
  api: ControlApi;
  tabId: number;
  onOpenModule?: ((module: string) => void) | undefined;
  pollMs?: number;
}) {
  const [flows, setFlows] = useState<FlowRecord[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [chip, setChip] = useState<Chip>('all');
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      // Per-tab by default: the question is what happened to *this* page.
      const page = await api.listFlows({ tab_id: tabId, limit: 300 });
      setFlows(page.flows);
      setError(null);
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  }, [api, tabId]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), pollMs);
    return () => clearInterval(timer);
  }, [refresh, pollMs]);

  const shown = useMemo(() => {
    switch (chip) {
      case 'modified':
        return flows.filter((f) => f.modified);
      case 'blocked':
        return flows.filter((f) => f.blocked);
      case 'warnings':
        return flows.filter((f) => severityOf(f) !== null);
      case 'unattributed':
        // Attribution gaps must be visible rather than mysterious (SPEC-3 §7.1).
        return flows.filter((f) => f.tab_id === null || f.tab_id === undefined);
      default:
        return flows;
    }
  }, [flows, chip]);

  const current = shown.find((f) => f.flow_id === selected) ?? null;

  return (
    <div className="panel">
      <div className="bar">
        {(['all', 'modified', 'blocked', 'warnings', 'unattributed'] as const).map((name) => (
          <button
            key={name}
            type="button"
            className="chip"
            aria-pressed={chip === name}
            onClick={() => setChip(name)}
          >
            {name}
          </button>
        ))}
        <span className="spacer" />
        <span className="dim">{shown.length} flows</span>
      </div>

      {error && <div className="err">Cannot reach the daemon: {error}</div>}

      <div className="split">
        <table className="flows">
          <thead>
            <tr>
              <th>Method</th>
              <th>Host</th>
              <th>Path</th>
              <th>Status</th>
              <th>Flags</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((flow) => (
              <tr
                key={flow.flow_id}
                className={flow.flow_id === selected ? 'sel' : undefined}
                onClick={() => setSelected(flow.flow_id)}
              >
                <td>{flow.request?.method ?? '—'}</td>
                <td>{flow.request?.host ?? flow.passthrough?.host ?? '—'}</td>
                <td className="path">{flow.request?.path ?? flow.passthrough?.pattern ?? '—'}</td>
                <td>{flow.response?.status ?? '—'}</td>
                <td>
                  {flow.blocked && <i className="f blk">BLK</i>}
                  {flow.modified && <i className="f mod">MOD</i>}
                  {severityOf(flow) === 'warning' && <i className="f warn">!</i>}
                  {severityOf(flow) === 'error' && <i className="f err">✕</i>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="detail">
          {current ? (
            <>
              <div className="detail-url">{current.request?.url ?? current.flow_id}</div>
              <Provenance flow={current} />
              <Headers title="Request headers" headers={current.request?.headers} />
              <Headers title="Response headers" headers={current.response?.headers} />
              {onOpenModule &&
                (current.provenance?.evaluated_modules ?? []).map((module: string) => (
                  <button
                    key={module}
                    type="button"
                    className="link"
                    onClick={() => onOpenModule(module)}
                  >
                    open {module} in the web UI
                  </button>
                ))}
            </>
          ) : (
            <p className="dim">
              {shown.length === 0
                ? 'No flows for this tab yet. Reload the page with the proxy on.'
                : 'Select a flow to see what pporlock did to it.'}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
