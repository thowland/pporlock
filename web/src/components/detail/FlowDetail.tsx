/**
 * Flow detail (SPEC-2 §6, REQ WUI-004).
 *
 * Opens as a side panel rather than a route change, so the table context is
 * retained — you are almost always comparing this flow against the ones around
 * it.
 */
import { useEffect, useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { FlowRecord, HeaderPairs } from '../../api/types';
import { formatBytes, formatMs, formatTime, statusClass } from '../../lib/format';
import { ProvenanceView } from './ProvenanceView';

type Tab = 'overview' | 'request' | 'response' | 'provenance';

const MASK_PATTERN = /^«redacted:sha1=([0-9a-f]{4}),len=(\d+)»$/;

/**
 * Masked values render distinctly and carry a stable hash prefix, so "is this
 * the same token" is answerable without unmasking (SPEC-0 §9.1).
 */
function HeaderValue({ value }: { value: string }) {
  const masked = MASK_PATTERN.exec(value);
  if (!masked) return <span className="hv">{value}</span>;
  return (
    <span className="hv masked" title={`redacted — ${masked[2]} bytes, fingerprint ${masked[1]}`}>
      redacted{' '}
      <span className="faint">
        ({masked[2]}b · {masked[1]})
      </span>
    </span>
  );
}

function Headers({ headers }: { headers: HeaderPairs | undefined }) {
  if (!headers || headers.length === 0) {
    return <div className="empty-small">No headers.</div>;
  }
  return (
    <table className="kv">
      <tbody>
        {headers.map(([name, value], index) => (
          <tr key={`${name}-${index}`}>
            <td className="k">{name}</td>
            <td className="v">
              <HeaderValue value={value} />
            </td>
          </tr>
        ))}
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
}: {
  flow: FlowRecord;
  api: ApiClient;
  onClose: () => void;
  onOpenModule?: ((module: string) => void) | undefined;
}) {
  const [tab, setTab] = useState<Tab>('provenance');
  const [full, setFull] = useState<FlowRecord>(flow);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setFull(flow);
    // The list carries summary detail; bodies and the full provenance chain are
    // fetched only when a flow is actually opened (SPEC-0 §6.3).
    setLoading(true);
    let cancelled = false;
    void api
      .getFlow(flow.flow_id, 'bodies')
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
  }, [api, flow]);

  const request = full.request;
  const response = full.response;
  const noteCount = full.provenance?.notes?.length ?? 0;

  return (
    <aside className="detail" aria-label="Flow detail">
      <header className="detail-head">
        <span className={`status ${statusClass(response?.status)}`}>{response?.status ?? '—'}</span>
        <span className="detail-url" title={request?.url ?? ''}>
          {request?.url ?? full.passthrough?.host ?? full.flow_id}
        </span>
        <span className="spacer" />
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
            <Headers headers={request?.headers} />
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
            <Headers headers={response?.headers} />
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
