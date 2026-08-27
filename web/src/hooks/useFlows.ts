/**
 * Live flow state (SPEC-2 §4.3, §5).
 *
 * Two things here are load-bearing for PRF-004:
 *
 * 1. Events are buffered and flushed on an animation frame. A busy page load
 *    delivers hundreds of events per second, and one React render per event
 *    will not keep up.
 * 2. Rows are keyed on flow_id and tolerate late field updates, because
 *    attribution backfills tab_id after the flow has already been delivered
 *    (SPEC-0 §3.6).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ApiClient } from '../api/client';
import { EventStream, type StreamState } from '../api/events';
import type { FlowFilter, FlowRecord } from '../api/types';

/** Client-side cap. The daemon's ring buffer is the real bound; this stops the
 *  page growing without limit if the buffer is configured very large. */
const MAX_ROWS = 2000;

interface UseFlowsResult {
  flows: FlowRecord[];
  streamState: StreamState;
  paused: boolean;
  heldCount: number;
  setPaused: (paused: boolean) => void;
  clear: () => void;
  refetch: (replace?: boolean) => void;
}

export function useFlows(api: ApiClient, filter: FlowFilter): UseFlowsResult {
  const [flows, setFlows] = useState<FlowRecord[]>([]);
  const [streamState, setStreamState] = useState<StreamState>('closed');
  const [paused, setPausedState] = useState(false);
  const [heldCount, setHeldCount] = useState(0);

  const pending = useRef<FlowRecord[]>([]);
  const frame = useRef<number | null>(null);
  const pausedRef = useRef(false);
  const streamRef = useRef<EventStream | null>(null);

  const filterKey = JSON.stringify(filter);

  const merge = useCallback((incoming: FlowRecord[]) => {
    setFlows((current) => {
      const byId = new Map(current.map((f) => [f.flow_id, f]));
      for (const record of incoming) {
        const existing = byId.get(record.flow_id);
        byId.set(record.flow_id, existing ? { ...existing, ...record } : record);
      }
      const merged = [...byId.values()];
      merged.sort((a, b) => (a.started_at < b.started_at ? 1 : -1));
      return merged.slice(0, MAX_ROWS);
    });
  }, []);

  const flush = useCallback(() => {
    frame.current = null;
    if (pending.current.length === 0) return;
    const batch = pending.current;
    pending.current = [];
    // Merge, not replace: a flow.updated carries only what changed (tab_id
    // backfill above all), so an existing row must survive it.
    merge(batch);
  }, [merge]);

  const schedule = useCallback(() => {
    if (frame.current !== null) return;
    // requestAnimationFrame is the right pacing in a visible tab, but it does
    // not exist in every environment the component can run in (jsdom without
    // pretendToBeVisual, a backgrounded tab). Falling back to a timer keeps the
    // batching behaviour rather than dropping the batch on the floor.
    if (typeof requestAnimationFrame === 'function') {
      frame.current = requestAnimationFrame(flush);
    } else {
      frame.current = setTimeout(flush, 16) as unknown as number;
    }
  }, [flush]);

  /**
   * Fetch the current page.
   *
   * `replace` distinguishes two cases that must not be conflated:
   *  - a gap refetch under an unchanged filter MERGES, because a fetch started
   *    before an event arrived resolves after it, and replacing would drop live
   *    flows that landed while the request was in flight;
   *  - a filter change REPLACES, because merging cannot remove rows and the new
   *    filter would appear not to narrow anything.
   */
  const refetch = useCallback(
    (replace = false) => {
      void api
        .listFlows(filter, { limit: 500, detail: 'summary' })
        .then((page) => {
          if (replace) setFlows(page.flows);
          else merge(page.flows);
        })
        .catch(() => {
          /* the status bar reports connection state; a failed refetch is not
           separately actionable here */
        });
    },
    [api, filterKey, merge], // eslint-disable-line react-hooks/exhaustive-deps
  );

  const setPaused = useCallback(
    (next: boolean) => {
      pausedRef.current = next;
      setPausedState(next);
      if (!next) {
        setHeldCount(0);
        schedule();
      }
    },
    [schedule],
  );

  const clear = useCallback(() => {
    pending.current = [];
    setFlows([]);
    setHeldCount(0);
  }, []);

  useEffect(() => {
    const stream = new EventStream(api);
    streamRef.current = stream;

    const offState = stream.onState(setStreamState);
    const offEvent = stream.on((event) => {
      if (event.type === 'stream.gap') {
        // We missed events. Refetch rather than silently showing a hole, and
        // merge so live flows arriving during the fetch are not lost.
        refetch(false);
        return;
      }
      if (event.type !== 'flow.completed' && event.type !== 'flow.updated') return;

      const record = event.data as unknown as FlowRecord;
      if (!record?.flow_id) return;

      if (pausedRef.current) {
        setHeldCount((n) => n + 1);
        return;
      }
      pending.current.push(record);
      schedule();
    });

    // The filter changed (or this is the first mount): discard rows that
    // belonged to the previous filter rather than merging into them.
    pending.current = [];
    stream.connect(filter);
    refetch(true);

    return () => {
      offEvent();
      offState();
      stream.disconnect();
      if (frame.current !== null) {
        if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(frame.current);
        else clearTimeout(frame.current);
      }
      frame.current = null;
    };
  }, [api, filterKey, refetch, schedule]); // eslint-disable-line react-hooks/exhaustive-deps

  return useMemo(
    () => ({ flows, streamState, paused, heldCount, setPaused, clear, refetch }),
    [flows, streamState, paused, heldCount, setPaused, clear, refetch],
  );
}
