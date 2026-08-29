/**
 * Flow detail (SPEC-2 §6, REQ WUI-004).
 *
 * Opens as a side panel rather than a route change, so the table context is
 * retained — you are almost always comparing this flow against the ones around
 * it.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { FlowRecord, HeaderPairs } from '../../api/types';
import { formatBytes, formatMs, formatTime, statusClass } from '../../lib/format';
import { headerFieldPath, parseMasked } from '../../lib/redaction';
import { ProvenanceView } from './ProvenanceView';
import { ExcludeHostAction } from '../exclusions/ExcludeHostAction';

type Tab = 'overview' | 'request' | 'response' | 'provenance';

/**
 * Reveals one masked value. `undefined` means "this flow cannot be unmasked" —
 * which is the case for every session flow (SPEC-0 §9.3, REQ CAP-045).
 */
export type UnmaskFn = (fieldPath: string) => Promise<string>;

/**
 * Masked values render distinctly and carry a stable hash prefix, so "is this
 * the same token" is answerable without unmasking (SPEC-0 §9.1).
 *
 * The reveal control exists **only** when `onUnmask` is supplied. That is the
 * whole gate: a session browser passes nothing and therefore cannot render one,
 * rather than rendering a disabled button that a future edit could re-enable.
 */
function HeaderValue({
  value,
  fieldPath,
  onUnmask,
}: {
  value: string;
  fieldPath: string;
  onUnmask?: UnmaskFn | undefined;
}) {
  const [revealed, setRevealed] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const masked = parseMasked(value);

  if (masked === null) return <span className="hv">{value}</span>;

  if (revealed !== null) {
    return (
      <span className="hv revealed">
        <span className="sr-only">revealed secret: </span>
        {revealed}{' '}
        <button type="button" className="linkish" onClick={() => setRevealed(null)}>
          hide
        </button>
      </span>
    );
  }

  return (
    <span className="hv masked">
      {/* Length and fingerprint, exactly as the extension shows them, so the
          two clients agree about what a masked value looks like. */}
      <span aria-hidden="true">🔒</span> redacted{' '}
      <span className="faint">
        ({masked.len} bytes · #{masked.sha1})
      </span>
      {onUnmask !== undefined && (
        <button
          type="button"
          className="linkish reveal"
          // Explicit per-value action, named so a screen reader user knows
          // which value is about to be revealed (REQ CAP-043, WUI-015).
          aria-label={`Reveal ${fieldPath}`}
          onClick={() => {
            setFailed(null);
            void onUnmask(fieldPath)
              .then(setRevealed)
              .catch((cause: unknown) =>
                setFailed(cause instanceof Error ? cause.message : 'Could not reveal this value.'),
              );
          }}
        >
          reveal
        </button>
      )}
      {failed !== null && (
        <span className="reveal-error" role="alert">
          {failed}
        </span>
      )}
    </span>
  );
}

function Headers({
  headers,
  side,
  onUnmask,
}: {
  headers: HeaderPairs | undefined;
  side: 'request' | 'response';
  onUnmask?: UnmaskFn | undefined;
}) {
  if (!headers || headers.length === 0) {
    return <div className="empty-small">No headers.</div>;
  }
  // The daemon addresses a repeated header by its index *among headers of that
  // name*, not by its row (redact.py:_from_headers). Counting here keeps the
  // field path right for the second Set-Cookie.
  const seen = new Map<string, number>();
  return (
    <table className="kv">
      <tbody>
        {headers.map(([name, value], index) => {
          const key = name.toLowerCase();
          const occurrence = seen.get(key) ?? 0;
          seen.set(key, occurrence + 1);
          return (
            <tr key={`${name}-${index}`}>
              <td className="k">{name}</td>
              <td className="v">
                <HeaderValue
                  value={value}
                  fieldPath={headerFieldPath(side, name, occurrence)}
                  onUnmask={onUnmask}
                />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Body({
  body,
  encoding,
  size,
  truncated,
  streamed,
}: {
  body: string | null | undefined;
  encoding: string | null | undefined;
  size: number | undefined;
  truncated: boolean | undefined;
  streamed?: boolean | undefined;
}) {
  if (streamed) {
    // Not "no body" — a body that was deliberately never held, which is a
    // different fact and the reason a transform may not have run.
    return (
      <div className="empty-small">
        This response was streamed, so its body was never buffered. Body transforms could not run on
        it.
      </div>
    );
  }
  if (body === null || body === undefined) {
    return (
      <div className="empty-small">
        Body not included at this detail level{size ? ` (${formatBytes(size)})` : ''}.
      </div>
    );
  }
  return (
    <>
      {truncated && (
        <div className="warn-strip">
          Truncated at the capture cap — the original was larger than shown.
        </div>
      )}
      <pre className="body">{encoding === 'base64' ? '(binary, base64)' : body}</pre>
    </>
  );
}

export function FlowDetail({
  flow,
  api,
  onClose,
  onOpenModule,
  onUnmask,
  loadDetail,
}: {
  flow: FlowRecord;
  api: ApiClient;
  onClose: () => void;
  onOpenModule?: ((module: string) => void) | undefined;
  /**
   * Supplied only where unmasking is legitimate: the **live ring buffer**
   * (REQ CAP-043). Omitted entirely by the session browser, because a session
   * flow was redacted before it reached the file and there is nothing to
   * reveal (REQ CAP-045). Absence removes the control, it does not disable it.
   */
  onUnmask?: UnmaskFn | undefined;
  /**
   * How to fetch the fuller representation of this flow. Defaults to
   * `GET /flows/{id}`; the session browser overrides it, since a recorded flow
   * is not in the live ring and that route would 404.
   */
  loadDetail?: ((flowId: string) => Promise<FlowRecord>) | undefined;
}) {
  const [tab, setTab] = useState<Tab>('provenance');
  const [full, setFull] = useState<FlowRecord>(flow);
  const [loading, setLoading] = useState(false);
  const panel = useRef<HTMLElement | null>(null);

  const fetchDetail = useCallback(
    (flowId: string) => (loadDetail ? loadDetail(flowId) : api.getFlow(flowId, 'bodies')),
    [api, loadDetail],
  );

  useEffect(() => {
    setFull(flow);
    // The list carries summary detail; bodies and the full provenance chain are
    // fetched only when a flow is actually opened (SPEC-0 §6.3).
    setLoading(true);
    let cancelled = false;
    void fetchDetail(flow.flow_id)
      .then((detailed) => {
        if (!cancelled) setFull(detailed);
      })
      .catch(() => {
        /* the summary record is still worth showing */
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fetchDetail, flow]);

  // Opening the panel moves focus into it, so a keyboard user is not left on a
  // table row behind a panel they cannot reach (REQ WUI-015). Focus returns to
  // the table on close — the shell owns that half, since it owns the rows.
  useEffect(() => {
    panel.current?.focus();
  }, []);

  const request = full.request;
  const response = full.response;
  const noteCount = full.provenance?.notes?.length ?? 0;

  return (
    <aside className="detail" aria-label="Flow detail" tabIndex={-1} ref={panel}>
      <header className="detail-head">
        <span className={`status ${statusClass(response?.status)}`}>{response?.status ?? '—'}</span>
        <span className="detail-url" title={request?.url ?? ''}>
          {request?.url ?? full.passthrough?.host ?? full.flow_id}
        </span>
        <span className="spacer" />
        {/* The same one-click exclusion as the table row (REQ PXY-016), here
            because the detail panel is where a user ends up when a host is
            misbehaving — and it is the only place a session flow's host is
            actionable at all. */}
        <ExcludeHostAction
          api={api}
          host={request?.host ?? full.passthrough?.host}
          surface="flow detail"
        />
        <button type="button" className="action" onClick={onClose} aria-label="Close detail">
          ✕
        </button>
      </header>

      <nav className="tabs" role="tablist">
        {(['provenance', 'overview', 'request', 'response'] as const).map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? 'tab active' : 'tab'}
            onClick={() => setTab(name)}
          >
            {name}
            {name === 'provenance' && noteCount > 0 && (
              <span className="tab-badge">{noteCount}</span>
            )}
          </button>
        ))}
      </nav>

      <div className="detail-body" role="tabpanel">
        {tab === 'provenance' && (
          <ProvenanceView provenance={full.provenance} onOpenModule={onOpenModule} />
        )}

        {tab === 'overview' && (
          <table className="kv">
            <tbody>
              <tr>
                <td className="k">flow</td>
                <td className="v mono">{full.flow_id}</td>
              </tr>
              <tr>
                <td className="k">kind</td>
                <td className="v">{full.kind}</td>
              </tr>
              <tr>
                <td className="k">started</td>
                <td className="v">{formatTime(full.started_at)}</td>
              </tr>
              <tr>
                <td className="k">tab</td>
                <td className="v">{full.tab_id ?? <span className="faint">unattributed</span>}</td>
              </tr>
              <tr>
                <td className="k">pporlock</td>
                <td className="v">{formatMs(full.timing?.pporlock_ms)}ms</td>
              </tr>
              <tr>
                <td className="k">flags</td>
                <td className="v">
                  {[
                    full.blocked && 'blocked',
                    full.modified && 'modified',
                    response?.streamed && 'streamed',
                    full.redacted && 'redacted',
                  ]
                    .filter(Boolean)
                    .join(', ') || <span className="faint">none</span>}
                </td>
              </tr>
              {full.kind === 'passthrough' && full.passthrough && (
                <>
                  <tr>
                    <td className="k">tunnelled</td>
                    <td className="v">{full.passthrough.host ?? full.passthrough.ip}</td>
                  </tr>
                  <tr>
                    <td className="k">matched</td>
                    <td className="v mono">{full.passthrough.pattern}</td>
                  </tr>
                  <tr>
                    <td className="k">why</td>
                    <td className="v">{full.passthrough.reason}</td>
                  </tr>
                </>
              )}
            </tbody>
          </table>
        )}

        {tab === 'request' && (
          <>
            <Headers headers={request?.headers} side="request" onUnmask={onUnmask} />
            <Body
              body={request?.body}
              encoding={request?.body_encoding}
              size={request?.body_size}
              truncated={request?.body_truncated}
            />
          </>
        )}

        {tab === 'response' && (
          <>
            <Headers headers={response?.headers} side="response" onUnmask={onUnmask} />
            <Body
              body={response?.body}
              encoding={response?.body_encoding}
              size={response?.body_size}
              truncated={response?.body_truncated}
              streamed={response?.streamed}
            />
          </>
        )}

        {loading && <div className="empty-small">Loading full detail…</div>}
      </div>
    </aside>
  );
}
