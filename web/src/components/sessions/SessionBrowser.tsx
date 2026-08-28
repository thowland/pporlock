/**
 * Browsing one recorded session (SPEC-2 §8.2, REQ CAP-021, WUI-010).
 *
 * "Using the same table and detail components as the live view — one
 * implementation, differing only in data source and the absence of unmasking."
 * That is not a style note. Two provenance views drift, and the moment they do,
 * the recorded explanation of a bug stops matching the live one.
 *
 * So this component owns exactly three things the live view does not: where the
 * flows come from, that there is no event stream, and that `onUnmask` is never
 * passed. FlowTable, FlowDetail and ProvenanceView are imported unchanged.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { FlowFilter, FlowRecord, SessionMeta } from '../../api/types';
import { FilterBar } from '../FilterBar';
import { FlowTable } from '../FlowTable';
import { FlowDetail } from '../detail/FlowDetail';
import { formatBytes, formatTime } from '../../lib/format';

interface Props {
  api: ApiClient;
  sessionId: string;
  onBack: () => void;
  onDryRun: (sessionId: string) => void;
  onOpenModule?: ((module: string) => void) | undefined;
}

/** One page is plenty for a diagnostic session, and bounds client memory. */
const PAGE_LIMIT = 500;

export function SessionBrowser({ api, sessionId, onBack, onDryRun, onOpenModule }: Props) {
  const [meta, setMeta] = useState<SessionMeta | null>(null);
  const [flows, setFlows] = useState<FlowRecord[] | null>(null);
  const [filter, setFilter] = useState<FlowFilter>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api
      .getSession(sessionId)
      .then((found) => {
        if (!cancelled) setMeta(found);
      })
      .catch(() => {
        /* the flow list below carries its own error */
      });
    return () => {
      cancelled = true;
    };
  }, [api, sessionId]);

  useEffect(() => {
    let cancelled = false;
    setFlows(null);
    // `full` rather than `bodies`: headers and the whole provenance chain, which
    // is what this screen is for, without pulling every recorded body into the
    // browser at once.
    void api
      .listSessionFlows(sessionId, filter, { limit: PAGE_LIMIT, detail: 'full' })
      .then((page) => {
        if (cancelled) return;
        setFlows(page.flows);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setFlows([]);
        setError(cause instanceof Error ? cause.message : 'Could not read this session.');
      });
    return () => {
      cancelled = true;
    };
  }, [api, sessionId, filter]);

  const selected = useMemo(
    () => (flows ?? []).find((f) => f.flow_id === selectedId) ?? null,
    [flows, selectedId],
  );

  // A recorded flow is not in the live ring, so `GET /flows/{id}` would 404.
  // The record from the session page is the whole record we have.
  const loadDetail = useCallback(
    (flowId: string) => {
      const found = (flows ?? []).find((f) => f.flow_id === flowId);
      return found ? Promise.resolve(found) : Promise.reject(new Error('not in this session'));
    },
    [flows],
  );

  useEffect(() => {
    if (selectedId === null) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelectedId(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selectedId]);

  const hasFilter = Object.keys(filter).length > 0;

  return (
    <div className="sessionbrowser">
      <div className="viewbar">
        <button type="button" className="action" onClick={onBack}>
          ← Sessions
        </button>
        <h2>{meta?.name ?? sessionId}</h2>
        {meta && (
          <span className="dim">
            {meta.flow_count} flows · {formatBytes(meta.size_bytes)} · profile {meta.profile} ·
            started {formatTime(meta.started_at)}
          </span>
        )}
        <span className="spacer" style={{ flex: 1 }} />
        <span className="pill dim" title="Session data was redacted before it was written to disk">
          🔒 redacted at write time — values cannot be revealed
        </span>
        <button type="button" className="action primary" onClick={() => onDryRun(sessionId)}>
          Dry run against this session
        </button>
      </div>

      {meta && meta.dropped > 0 && (
        <div className="banner warn" role="status">
          ⚠ {meta.dropped} flows were dropped while recording, so this session is incomplete. A dry
          run against it will not see them.
        </div>
      )}

      {error !== null && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}

      <FilterBar filter={filter} onChange={setFilter} />

      {flows === null ? (
        <div className="empty">Loading session flows…</div>
      ) : (
        <div className="layout">
          <div className="tablewrap">
            <FlowTable
              flows={flows}
              // A session is never "disconnected" — it is a file. Passing true
              // keeps the empty state honest: no flows here means no flows were
              // recorded, not that we cannot see them.
              connected
              hasFilter={hasFilter}
              selectedId={selectedId}
              onSelect={(flow: FlowRecord) => setSelectedId(flow.flow_id)}
            />
          </div>
          {selected && (
            <FlowDetail
              flow={selected}
              api={api}
              loadDetail={loadDetail}
              onClose={() => setSelectedId(null)}
              onOpenModule={onOpenModule}
              // No `onUnmask`. Deliberately, structurally: unmasking is
              // live-ring-only (SPEC-0 §9.3) and a recorded flow never held the
              // real value to begin with (REQ CAP-045). There is nothing here
              // to reveal, so there is no control to press.
            />
          )}
        </div>
      )}
    </div>
  );
}
