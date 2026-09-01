/**
 * The persistent status bar (SPEC-2 §3.2).
 *
 * This is where the system tells the truth about its own state. It exists
 * because most of this tool's failure modes are invisible in the page being
 * debugged: a development toggle silently changing traffic, a module
 * quarantined, the daemon gone.
 */
import type { StreamState } from '../api/events';
import type { Connection } from '../hooks/useDaemonState';
import type { DaemonState } from '../api/types';
import { formatBytes } from '../lib/format';

interface Props {
  state: DaemonState | null;
  connection: Connection;
  streamState: StreamState;
  flowCount: number;
}

function connectionPill(connection: Connection, streamState: StreamState) {
  if (connection === 'disconnected') {
    return (
      <span className="pill error">
        <span className="dot" />
        disconnected
      </span>
    );
  }
  if (connection === 'unauthorized') {
    return (
      <span className="pill warn">
        <span className="dot" />
        not paired
      </span>
    );
  }
  if (streamState === 'open') {
    return (
      <span className="pill ok">
        <span className="dot" />
        live
      </span>
    );
  }
  if (streamState === 'reconnecting' || streamState === 'connecting') {
    return (
      <span className="pill warn">
        <span className="dot" />
        {streamState}
      </span>
    );
  }
  return (
    <span className="pill dim">
      <span className="dot" />
      idle
    </span>
  );
}

export function StatusBar({ state, connection, streamState, flowCount }: Props) {
  const toggles = state?.dev_toggles;
  const activeToggles = toggles
    ? Object.entries(toggles)
        .filter(([, on]) => on)
        .map(([name]) => name)
    : [];

  return (
    <div className="statusbar">
      {/* The same file the extension draws in the toolbar, byte for byte —
          `poppy.test.ts` asserts it. `alt=""` because the word beside it says
          the same thing: a screen reader announcing "pporlock pporlock" is the
          usual cost of a decorative mark given a name. */}
      <img className="mark" src="/poppy.svg" alt="" width={16} height={16} />
      <span className="brand">pporlock</span>
      {connectionPill(connection, streamState)}

      {state && (
        <span className="pill dim" title="Proxy listen address">
          {state.proxy.listen}
        </span>
      )}

      {/* A dev toggle makes production behaviour unreproducible, so it is
          never subtle (REQ WUI-012, PXY-044). */}
      {activeToggles.length > 0 && (
        <span
          className="pill devtoggle"
          title="Development toggles alter traffic. Turn them off for normal use."
        >
          ⚠ {activeToggles.join(' + ')} active
        </span>
      )}

      <span className="spacer" />

      {state && (
        <>
          <span className="stat">
            flows <b>{state.counters.flows_total.toLocaleString()}</b>
          </span>
          <span className="stat">
            blocked <b>{state.counters.blocked.toLocaleString()}</b>
          </span>
          <span className="stat">
            tunneled <b>{state.counters.passthrough.toLocaleString()}</b>
          </span>
          <span className="stat" title="Ring buffer occupancy">
            buffer <b>{state.capture.ring_flows.toLocaleString()}</b>
            <span className="faint"> / {formatBytes(state.capture.ring_bytes)}</span>
          </span>
        </>
      )}
      <span className="stat">
        shown <b>{flowCount.toLocaleString()}</b>
      </span>
      {state && <span className="stat faint">v{state.version}</span>}
    </div>
  );
}
