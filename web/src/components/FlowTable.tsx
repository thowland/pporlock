/**
 * The live flow table (SPEC-2 §5.1, REQ WUI-003).
 *
 * Windowed rather than fully virtualized: the daemon's ring buffer is the hard
 * bound on how much there is to show, and the client keeps at most that. What
 * matters for PRF-004 is that a busy page load does not cause one React render
 * per event — that is handled upstream in useFlows, which buffers and flushes
 * on an animation frame.
 */
import type { FlowRecord } from '../api/types';
import { formatBytes, formatMs, formatTime, shortType, statusClass } from '../lib/format';

interface Props {
  flows: FlowRecord[];
  connected: boolean;
  hasFilter: boolean;
}

function noteSeverity(flow: FlowRecord): 'error' | 'warning' | null {
  const notes = flow.provenance?.notes ?? [];
  if (notes.some((n) => n.severity === 'error')) return 'error';
  if (notes.some((n) => n.severity === 'warning')) return 'warning';
  return null;
}

/**
 * The flags column is the density payoff: it is how you scan a hundred flows
 * for the one that went wrong.
 */
function Flags({ flow, attributionActive }: { flow: FlowRecord; attributionActive: boolean }) {
  const severity = noteSeverity(flow);
  return (
    <span className="flags">
      {flow.kind === 'passthrough' && (
        <span className="flag tunnel" title="Tunneled undecrypted — excluded host">
          TUN
        </span>
      )}
      {flow.blocked && (
        <span className="flag blocked" title="Short-circuited by a rule">
          BLK
        </span>
      )}
      {flow.modified && (
        <span className="flag modified" title="Headers or body were changed">
          MOD
        </span>
      )}
      {flow.response?.streamed && (
        <span className="flag streamed" title="Streamed — body transforms unavailable">
          STR
        </span>
      )}
      {severity === 'warning' && (
        <span className="flag warn" title="Warning notes on this flow">
          !
        </span>
      )}
      {severity === 'error' && (
        <span className="flag error" title="Error notes on this flow">
          ✕
        </span>
      )}
      {attributionActive && flow.tab_id === null && flow.kind === 'http' && (
        <span className="flag unattributed" title="No tab attributed to this flow">
          ?
        </span>
      )}
    </span>
  );
}

function EmptyState({ connected, hasFilter }: { connected: boolean; hasFilter: boolean }) {
  if (!connected) {
    // Never show an empty table that looks like quiet traffic (REQ WUI-013).
    return (
      <div className="empty">
        <h2>Not connected to the daemon</h2>
        <p>The table below is empty because there is nothing to read, not because</p>
        <p>nothing is happening.</p>
      </div>
    );
  }
  if (hasFilter) {
    return (
      <div className="empty">
        <h2>No flows match this filter</h2>
        <p>Traffic may still be arriving — clear the filter to see it.</p>
      </div>
    );
  }
  return (
    <div className="empty">
      <h2>Waiting for traffic</h2>
      <p>Browse with Chrome pointed at the proxy and flows will appear here live.</p>
    </div>
  );
}

export function FlowTable({ flows, connected, hasFilter }: Props) {
  if (flows.length === 0) {
    return <EmptyState connected={connected} hasFilter={hasFilter} />;
  }

  // Until the extension supplies attribution (Sprint 6), every flow is
  // unattributed — so the marker would appear on every row and mean nothing.
  // A flag that is always on trains the eye to ignore it, so it only appears
  // once attribution is actually producing results.
  const attributionActive = flows.some((f) => f.tab_id !== null && f.tab_id !== undefined);

  return (
    <table className="flows">
      <thead>
        <tr>
          <th>Time</th>
          <th>Method</th>
          <th>Host</th>
          <th>Path</th>
          <th className="num">Status</th>
          <th>Type</th>
          <th className="num">Size</th>
          <th className="num" title="Time spent inside pporlock's pipeline">
            pporlock
          </th>
          <th>Flags</th>
        </tr>
      </thead>
      <tbody>
        {flows.map((flow) => {
          const request = flow.request;
          const response = flow.response;
          const overhead = flow.timing?.pporlock_ms ?? null;
          return (
            <tr key={flow.flow_id}>
              <td className="dim">{formatTime(flow.started_at)}</td>
              <td>{request?.method ?? <span className="faint">—</span>}</td>
              <td>{flow.kind === 'passthrough' ? flow.passthrough?.host : request?.host}</td>
              <td>
                <span className="path" title={request?.url ?? flow.passthrough?.pattern ?? ''}>
                  <span>{request?.path ?? flow.passthrough?.pattern ?? '—'}</span>
                </span>
              </td>
              <td
                className={`num ${statusClass(response?.status)} ${flow.blocked ? 'synthesized' : ''}`}
              >
                {response?.status ?? '—'}
              </td>
              <td className="dim">{shortType(response?.content_type)}</td>
              <td className="num dim">{formatBytes(response?.body_size)}</td>
              <td
                className={`num overhead ${overhead !== null && overhead > 50 ? 'slow' : ''}`}
                title="Proxy overhead for this flow"
              >
                {formatMs(overhead)}
              </td>
              <td>
                <Flags flow={flow} attributionActive={attributionActive} />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
