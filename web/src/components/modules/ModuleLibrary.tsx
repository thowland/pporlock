/**
 * The module library (SPEC-2 §7.1, REQ WUI-005).
 *
 * Load errors and quarantine reasons render inline and expanded, with the
 * traceback — never behind a click. A module that failed to load is the thing
 * the user came to this page to find, and hiding it one interaction away is how
 * a debugging tool becomes the thing being debugged.
 */
import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { ModuleStatus } from '../../api/types';

interface Props {
  api: ApiClient;
  onOpen: (name: string) => void;
}

const STATE_LABEL: Record<ModuleStatus['state'], string> = {
  loaded: 'loaded',
  disabled: 'disabled',
  quarantined: 'quarantined',
  load_error: 'load error',
};

/** Sorted by effective run order: lower priority runs earlier (SPEC-0 §5.4). */
function byPriority(a: ModuleStatus, b: ModuleStatus): number {
  return a.priority - b.priority || a.name.localeCompare(b.name);
}

export function ModuleLibrary({ api, onOpen }: Props) {
  const [modules, setModules] = useState<ModuleStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await api.listModules();
      setModules([...list].sort(byPriority));
      setError(null);
    } catch (cause) {
      setModules([]);
      setError(cause instanceof Error ? cause.message : 'Could not list modules.');
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const patch = async (name: string, changes: { enabled?: boolean; priority?: number }) => {
    setBusy(name);
    try {
      await api.patchModule(name, changes);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : `Could not update ${name}.`);
    } finally {
      setBusy(null);
    }
  };

  /**
   * Reordering writes `priority` back, because priority — not list position —
   * is what the daemon evaluates (SPEC-0 §5.4). Swapping with the neighbour
   * keeps every other module's number untouched.
   */
  const move = (index: number, delta: number) => {
    if (modules === null) return;
    // Numeric positions into our own array, bounds-checked immediately below;
    // no property name here comes from user input.
    /* eslint-disable security/detect-object-injection */
    const current = modules[index];
    const neighbour = modules[index + delta];
    /* eslint-enable security/detect-object-injection */
    if (current === undefined || neighbour === undefined) return;
    const target =
      current.priority === neighbour.priority
        ? neighbour.priority + (delta < 0 ? -1 : 1)
        : neighbour.priority;
    void patch(current.name, { priority: target });
  };

  if (modules === null) {
    return <div className="empty">Loading modules…</div>;
  }

  return (
    <div className="modulelib">
      <div className="viewbar">
        <h2>Modules</h2>
        <span className="spacer" style={{ flex: 1 }} />
        <button type="button" className="action" onClick={() => void refresh()}>
          Refresh
        </button>
        <button
          type="button"
          className="action"
          onClick={() => void api.reloadModules().then(refresh, refresh)}
        >
          Reload all
        </button>
      </div>

      {error !== null && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}

      {modules.length === 0 ? (
        <div className="empty">
          <h2>No modules</h2>
          <p>Create one from a flow in the traffic view, or author it here.</p>
        </div>
      ) : (
        <table className="modules">
          <thead>
            <tr>
              <th>Enabled</th>
              <th>Name</th>
              <th>Version</th>
              <th className="num">Priority</th>
              <th>State</th>
              <th className="num">Rules</th>
              <th>Python</th>
              <th className="num">Matched</th>
              <th className="num">Modified</th>
              <th className="num">Errors</th>
              <th className="num">Avg ms</th>
              <th>Order</th>
            </tr>
          </thead>
          <tbody>
            {modules.map((module, index) => (
              <ModuleRow
                key={module.name}
                module={module}
                busy={busy === module.name}
                onOpen={onOpen}
                onToggle={(enabled) => void patch(module.name, { enabled })}
                onPriority={(priority) => void patch(module.name, { priority })}
                onMove={(delta) => move(index, delta)}
                canMoveUp={index > 0}
                canMoveDown={index < modules.length - 1}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ModuleRow({
  module,
  busy,
  onOpen,
  onToggle,
  onPriority,
  onMove,
  canMoveUp,
  canMoveDown,
}: {
  module: ModuleStatus;
  busy: boolean;
  onOpen: (name: string) => void;
  onToggle: (enabled: boolean) => void;
  onPriority: (priority: number) => void;
  onMove: (delta: number) => void;
  canMoveUp: boolean;
  canMoveDown: boolean;
}) {
  const problem = module.error ?? null;
  const quarantine = module.quarantine ?? null;

  return (
    <>
      <tr className={`module ${module.state}`}>
        <td>
          <input
            type="checkbox"
            aria-label={`Enable ${module.name}`}
            checked={module.enabled}
            disabled={busy}
            onChange={(event) => onToggle(event.target.checked)}
          />
        </td>
        <td>
          <button type="button" className="linkish" onClick={() => onOpen(module.name)}>
            {module.name}
          </button>
        </td>
        <td className="dim">{module.version}</td>
        <td className="num">
          <PriorityField module={module} busy={busy} onCommit={onPriority} />
        </td>
        <td>
          <span className={`pill state-${module.state}`}>
            {/* Never colour alone (REQ WUI-015): the badge always carries text. */}
            {STATE_LABEL[module.state]}
          </span>
        </td>
        <td className="num">{module.rule_count}</td>
        <td className="dim">{module.has_python ? 'yes' : '—'}</td>
        {/* An em dash rather than 0: "no statistics yet" and "matched nothing"
            are different facts, and showing 0 for the first is a lie. */}
        <td className="num dim">{module.stats?.flows_matched.toLocaleString() ?? '—'}</td>
        <td className="num dim">{module.stats?.flows_modified.toLocaleString() ?? '—'}</td>
        <td className={`num ${(module.stats?.errors ?? 0) > 0 ? 'bad' : 'dim'}`}>
          {module.stats?.errors ?? '—'}
        </td>
        <td className="num dim">{module.stats?.avg_ms.toFixed(2) ?? '—'}</td>
        <td>
          <button
            type="button"
            className="action"
            aria-label={`Move ${module.name} earlier`}
            disabled={!canMoveUp || busy}
            onClick={() => onMove(-1)}
          >
            ↑
          </button>
          <button
            type="button"
            className="action"
            aria-label={`Move ${module.name} later`}
            disabled={!canMoveDown || busy}
            onClick={() => onMove(1)}
          >
            ↓
          </button>
        </td>
      </tr>

      {problem !== null && (
        <tr className="module-problem">
          <td colSpan={12}>
            <div className="banner error" role="alert">
              <strong>{problem.code}</strong> {problem.message}
              {typeof problem.line === 'number' && (
                <span className="faint"> (module.yaml line {problem.line})</span>
              )}
              {problem.trace !== null && problem.trace !== undefined && problem.trace !== '' && (
                <pre className="trace">{problem.trace}</pre>
              )}
            </div>
          </td>
        </tr>
      )}

      {quarantine !== null && (
        <tr className="module-problem">
          <td colSpan={12}>
            <div className="banner warn" role="alert">
              <strong>Quarantined</strong> after {quarantine.failures} consecutive failures since{' '}
              {quarantine.since}: {quarantine.reason}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * Priority commits on blur or Enter, not on keystroke.
 *
 * A controlled field wired straight to `PATCH` would fire a request per
 * character — and would briefly send `priority: 0` the moment the user cleared
 * it to type a new number.
 */
function PriorityField({
  module,
  busy,
  onCommit,
}: {
  module: ModuleStatus;
  busy: boolean;
  onCommit: (priority: number) => void;
}) {
  const [text, setText] = useState(String(module.priority));
  const [seen, setSeen] = useState(module.priority);

  if (seen !== module.priority) {
    setSeen(module.priority);
    setText(String(module.priority));
  }

  const commit = () => {
    const next = Number(text);
    if (Number.isFinite(next) && text.trim() !== '' && next !== module.priority) onCommit(next);
    else setText(String(module.priority));
  };

  return (
    <input
      className="prio"
      type="number"
      aria-label={`Priority for ${module.name}`}
      value={text}
      disabled={busy}
      onChange={(event) => setText(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === 'Enter') commit();
      }}
    />
  );
}
