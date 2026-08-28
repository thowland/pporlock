/**
 * Filter controls (SPEC-2 §5.2).
 *
 * The vocabulary is exactly SPEC-0 §6.5 — no more and no less — so a filter set
 * is transferable between this table, the DevTools panel, and the MCP tools.
 * Filters are pushed to the server for both the query and the subscription, so
 * a narrow filter reduces event volume rather than merely hiding rows.
 */
import type { FlowFilter } from '../api/types';

interface Props {
  filter: FlowFilter;
  onChange: (next: FlowFilter) => void;
  /**
   * Live controls (SPEC-2 §5.3) belong to a stream. A recorded session is not
   * one: pausing a file and clearing a file are both meaningless, so the
   * session browser reuses the same filter vocabulary with them omitted.
   */
  paused?: boolean | undefined;
  heldCount?: number | undefined;
  onTogglePause?: (() => void) | undefined;
  onClear?: (() => void) | undefined;
}

export function FilterBar({ filter, onChange, paused, heldCount, onTogglePause, onClear }: Props) {
  const held = heldCount ?? 0;
  const set = <K extends keyof FlowFilter>(key: K, value: FlowFilter[K]) => {
    const next = { ...filter };
    // security/detect-object-injection flags dynamic keys, but K is constrained
    // to keyof FlowFilter at the type level and every call site passes a
    // literal — there is no path here from user input to a property name.
    if (value === undefined || value === '' || value === false) {
      // eslint-disable-next-line security/detect-object-injection
      delete next[key];
    } else {
      // eslint-disable-next-line security/detect-object-injection
      next[key] = value;
    }
    onChange(next);
  };

  return (
    <div className="filterbar">
      <input
        type="text"
        placeholder="host"
        aria-label="Filter by host"
        value={filter.host ?? ''}
        onChange={(e) => set('host', e.target.value || undefined)}
      />
      <input
        type="text"
        placeholder="path regex"
        aria-label="Filter by path regex"
        value={filter.path ?? ''}
        onChange={(e) => set('path', e.target.value || undefined)}
      />
      <input
        type="text"
        placeholder="status"
        aria-label="Filter by status"
        value={filter.status ?? ''}
        onChange={(e) => set('status', e.target.value || undefined)}
      />

      <button
        type="button"
        className="chip"
        aria-pressed={filter.modified === true}
        onClick={() => set('modified', filter.modified ? undefined : true)}
      >
        modified
      </button>
      <button
        type="button"
        className="chip"
        aria-pressed={filter.blocked === true}
        onClick={() => set('blocked', filter.blocked ? undefined : true)}
      >
        blocked
      </button>

      <span className="spacer" style={{ flex: 1 }} />

      {onTogglePause !== undefined && (
        <button type="button" className="action" onClick={onTogglePause}>
          {paused ? `resume${held > 0 ? ` (${held} held)` : ''}` : 'pause'}
        </button>
      )}
      {onClear !== undefined && (
        <button type="button" className="action" onClick={onClear}>
          clear
        </button>
      )}
    </div>
  );
}
