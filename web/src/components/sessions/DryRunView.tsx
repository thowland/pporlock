/**
 * Dry run (SPEC-2 §8.3, REQ WUI-010, CAP-030, CAP-032, CAP-033).
 *
 * Two things this screen must not soften.
 *
 * The first is that **dry run executes the candidate module's Python code**.
 * "It touches no live traffic" is true and is not the same claim. For a module
 * an agent wrote and nobody read, pressing this button runs unread code on the
 * user's machine, which is why the warning is permanent and above the control
 * rather than a tooltip beside it (REQ CAP-032, MOD-031).
 *
 * The second is that the interesting result is what *changed*. "My rule matched
 * nothing" is the most common dry-run outcome, so the unaffected flows are
 * collapsed but counted — never quietly omitted, which would make a run that
 * did nothing look identical to a run that did everything.
 */
import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../../api/client';
import type {
  DryRunFlowResult,
  DryRunRequest,
  DryRunResult,
  ModuleStatus,
  SessionMeta,
} from '../../api/types';
import { ProvenanceView } from '../detail/ProvenanceView';

interface Props {
  api: ApiClient;
  sessionId: string;
  onBack: () => void;
  onOpenModule?: ((module: string) => void) | undefined;
  /** Pre-selects a module — the editor's "Dry run" jumps here (SPEC-2 §7.2). */
  initialModule?: string | undefined;
}

const DEFAULT_LIMIT = 500;

function Summary({ summary }: { summary: DryRunResult['summary'] }) {
  const cells: [string, string, string][] = [
    ['flows evaluated', String(summary.flows_evaluated), ''],
    ['matched', String(summary.matched), ''],
    ['modified', String(summary.modified), 'ok'],
    ['blocked', String(summary.blocked), 'warn'],
    ['errors', String(summary.errors), summary.errors > 0 ? 'error' : ''],
    ['avg', `${summary.avg_ms.toFixed(2)}ms`, ''],
    ['p95', `${summary.p95_ms.toFixed(2)}ms`, ''],
  ];
  return (
    <dl className="dryrun-summary" aria-label="Dry run summary">
      {cells.map(([label, value, tone]) => (
        <div key={label} className={tone ? `cell ${tone}` : 'cell'}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * The diff for one flow. Rendered as text, never as markup: this string is
 * derived from captured response bodies, so treating it as HTML would let a
 * page under test inject into the tool inspecting it.
 */
function Diff({ result }: { result: DryRunFlowResult }) {
  const diff = result.diff;
  if (!diff) return null;
  const headers = diff.headers ?? [];
  const body = diff.body ?? null;
  if (headers.length === 0 && body === null) return null;
  return (
    <div className="dryrun-diff">
      {headers.length > 0 && (
        <table className="kv">
          <caption className="sr-only">Header changes</caption>
          <tbody>
            {headers.map((change, index) => (
              <tr key={`${change.op}-${change.name}-${index}`}>
                <td className="k">
                  <span className={`diff-op op-${change.op}`}>{change.op}</span>
                </td>
                <td className="v">
                  {change.name}
                  {change.value !== undefined && change.value !== null && (
                    <span className="faint"> = {change.value}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {body !== null && (
        <>
          {body.truncated && (
            <div className="warn-strip">
              The diff was truncated — the change is larger than shown.
            </div>
          )}
          <pre className="body diff">{body.text}</pre>
        </>
      )}
    </div>
  );
}

export function DryRunView({ api, sessionId, onBack, onOpenModule, initialModule }: Props) {
  const [meta, setMeta] = useState<SessionMeta | null>(null);
  const [modules, setModules] = useState<ModuleStatus[]>([]);
  const [selected, setSelected] = useState<string>(initialModule ?? '');
  // Held as text so the field can be empty mid-edit. Coercing an empty box
  // back to the default would silently rewrite what the user is typing.
  const [limit, setLimit] = useState<string>(String(DEFAULT_LIMIT));
  const [includeDiffs, setIncludeDiffs] = useState(true);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<DryRunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showUnaffected, setShowUnaffected] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    void api
      .getSession(sessionId)
      .then(setMeta)
      .catch(() => setMeta(null));
    void api
      .listModules()
      .then((list) => {
        setModules(list);
        // Pre-select only when the caller did not: an arbitrary default would
        // make "run" execute code the user did not choose.
        setSelected((current) => current || (list[0]?.name ?? ''));
      })
      .catch(() => setModules([]));
  }, [api, sessionId]);

  const run = useCallback(() => {
    if (selected === '') {
      setError('Choose a module to evaluate.');
      return;
    }
    const parsed = Number.parseInt(limit, 10);
    const request: DryRunRequest = {
      use_installed: [selected],
      limit: Number.isNaN(parsed) || parsed < 1 ? DEFAULT_LIMIT : parsed,
      include_diffs: includeDiffs,
    };
    setRunning(true);
    setError(null);
    void api
      .dryRun(sessionId, request)
      .then((outcome) => {
        setResult(outcome);
        setShowUnaffected(false);
      })
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : 'The dry run failed.'),
      )
      .finally(() => setRunning(false));
  }, [api, sessionId, selected, limit, includeDiffs]);

  const unaffected =
    result === null ? 0 : Math.max(0, result.summary.flows_evaluated - result.summary.matched);

  return (
    <div className="dryrun">
      <div className="viewbar">
        <button type="button" className="action" onClick={onBack}>
          ← Sessions
        </button>
        <h2>Dry run · {meta?.name ?? sessionId}</h2>
      </div>

      {/* Permanent, above the control, and phrased as what happens rather than
          as a caution (REQ CAP-032, MOD-031). */}
      <div className="banner warn dryrun-warning" role="note">
        <strong>⚠ Dry run executes the module&apos;s Python code.</strong> Module code is trusted
        and unsandboxed by design. No live traffic is touched, but the module&apos;s hooks run on
        this machine against the recorded flows. If an AI agent wrote this module, read it before
        you run it.
      </div>

      <div className="dryrun-controls">
        <label htmlFor="dryrun-module">Module</label>
        <select
          id="dryrun-module"
          value={selected}
          onChange={(event) => setSelected(event.target.value)}
        >
          <option value="">— choose a module —</option>
          {modules.map((module) => (
            <option key={module.name} value={module.name}>
              {module.name} {module.enabled ? '' : '(disabled)'}
            </option>
          ))}
        </select>

        <label htmlFor="dryrun-limit">Flows</label>
        <input
          id="dryrun-limit"
          type="number"
          min={1}
          max={5000}
          value={limit}
          onChange={(event) => setLimit(event.target.value)}
        />

        <label htmlFor="dryrun-diffs">
          <input
            id="dryrun-diffs"
            type="checkbox"
            checked={includeDiffs}
            onChange={(event) => setIncludeDiffs(event.target.checked)}
          />
          Include diffs
        </label>

        <button type="button" className="action primary" onClick={run} disabled={running}>
          {running ? 'Running…' : 'Run and execute module code'}
        </button>
      </div>

      {error !== null && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}

      {result !== null && (
        <div className="dryrun-results" aria-live="polite">
          <Summary summary={result.summary} />

          {result.results_note && <div className="empty-small">{result.results_note}</div>}

          {result.results.length === 0 ? (
            <div className="empty">
              <h2>Nothing was affected</h2>
              <p>{result.summary.flows_evaluated} flows were evaluated and none of them matched.</p>
            </div>
          ) : (
            result.results.map((flow) => {
              const open = expanded === flow.flow_id;
              return (
                <section key={flow.flow_id} className="dryrun-result">
                  <h3>
                    <button
                      type="button"
                      className="linkish"
                      aria-expanded={open}
                      onClick={() => setExpanded(open ? null : flow.flow_id)}
                    >
                      {open ? '▾' : '▸'} {flow.url}
                    </button>
                  </h3>
                  {open && (
                    <>
                      <ProvenanceView provenance={flow.provenance} onOpenModule={onOpenModule} />
                      <Diff result={flow} />
                    </>
                  )}
                </section>
              );
            })
          )}

          {/* Collapsed by default, but counted: a run that changed nothing must
              not look like a run that was never made (SPEC-2 §8.3). */}
          <section className="dryrun-unaffected">
            <button
              type="button"
              className="linkish"
              aria-expanded={showUnaffected}
              onClick={() => setShowUnaffected(!showUnaffected)}
            >
              {showUnaffected ? '▾' : '▸'} {unaffected} unaffected flows
            </button>
            {showUnaffected && (
              <p className="empty-small">
                These {unaffected} of {result.summary.flows_evaluated} evaluated flows matched no
                rule in this module, so nothing would have changed for them. Browse the session to
                see them in full.
              </p>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
