import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from '../api/client';
import type { FlowRecord } from '../api/types';
import { useFlows } from './useFlows';
import { makeFlow } from '../test/factories';

type Handler = (event: { type: string; data: unknown }) => void;

const handlers: Handler[] = [];

vi.mock('../api/events', () => ({
  EventStream: class {
    on(handler: Handler) {
      handlers.push(handler);
      return () => {
        const i = handlers.indexOf(handler);
        if (i >= 0) handlers.splice(i, 1);
      };
    }
    onState() {
      return () => {};
    }
    connect() {}
    disconnect() {}
    get state() {
      return 'open';
    }
  },
}));

function emit(type: string, data: unknown) {
  for (const handler of [...handlers]) handler({ type, data });
}

function apiWith(flows: FlowRecord[]): ApiClient {
  const api = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(api, 'listFlows').mockResolvedValue({
    flows,
    next_cursor: null,
    total_estimate: flows.length,
  });
  return api;
}

/** Stable identities. A new object per render churns the subscription effect. */
const EMPTY_FILTER = {};

afterEach(() => {
  handlers.length = 0;
  vi.restoreAllMocks();
});

describe('useFlows', () => {
  it('seeds from the initial fetch', async () => {
    const api = apiWith([makeFlow({ flow_id: 'a' })]);
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    await waitFor(() => expect(result.current.flows).toHaveLength(1));
  });

  it('appends flows arriving over the stream', async () => {
    const api = apiWith([]);
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    await waitFor(() => expect(result.current.flows).toHaveLength(0));

    act(() => emit('flow.completed', makeFlow({ flow_id: 'b' })));
    await waitFor(() => expect(result.current.flows).toHaveLength(1));
  });

  it('merges a flow.updated into the existing row', async () => {
    // Attribution backfills tab_id after delivery (SPEC-0 §3.6), so rows must
    // be keyed on flow_id and tolerate late field updates.
    const api = apiWith([makeFlow({ flow_id: 'a', tab_id: null })]);
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    await waitFor(() => expect(result.current.flows).toHaveLength(1));

    act(() => emit('flow.updated', { ...makeFlow({ flow_id: 'a' }), tab_id: 481 }));
    await waitFor(() => expect(result.current.flows[0]?.tab_id).toBe(481));
    expect(result.current.flows).toHaveLength(1);
  });

  it('refetches on stream.gap rather than showing a hole', async () => {
    const api = apiWith([]);
    renderHook(() => useFlows(api, EMPTY_FILTER));
    await waitFor(() => expect(api.listFlows).toHaveBeenCalledTimes(1));

    act(() => emit('stream.gap', { from_seq: 1, to_seq: 9 }));
    await waitFor(() => expect(api.listFlows).toHaveBeenCalledTimes(2));
  });

  it('holds events while paused and counts them', async () => {
    const api = apiWith([]);
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    await waitFor(() => expect(result.current.flows).toHaveLength(0));

    act(() => result.current.setPaused(true));
    act(() => emit('flow.completed', makeFlow({ flow_id: 'held' })));

    await waitFor(() => expect(result.current.heldCount).toBe(1));
    expect(result.current.flows).toHaveLength(0);
  });

  it('resumes cleanly', async () => {
    const api = apiWith([]);
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    act(() => result.current.setPaused(true));
    act(() => emit('flow.completed', makeFlow({ flow_id: 'held' })));
    await waitFor(() => expect(result.current.heldCount).toBe(1));

    act(() => result.current.setPaused(false));
    await waitFor(() => expect(result.current.heldCount).toBe(0));
    expect(result.current.paused).toBe(false);
  });

  it('clears', async () => {
    const api = apiWith([makeFlow()]);
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    await waitFor(() => expect(result.current.flows).toHaveLength(1));
    act(() => result.current.clear());
    expect(result.current.flows).toHaveLength(0);
  });

  it('ignores events that are not flows', async () => {
    const api = apiWith([]);
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    act(() => emit('state.changed', { anything: true }));
    await new Promise((r) => setTimeout(r, 30));
    expect(result.current.flows).toHaveLength(0);
  });

  it('ignores a malformed flow payload', async () => {
    const api = apiWith([]);
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    act(() => emit('flow.completed', { no_flow_id: true }));
    await new Promise((r) => setTimeout(r, 30));
    expect(result.current.flows).toHaveLength(0);
  });

  it('sorts newest first', async () => {
    const api = apiWith([]);
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    act(() => {
      emit('flow.completed', makeFlow({ flow_id: 'old', started_at: '2026-08-27T10:00:00.000Z' }));
      emit('flow.completed', makeFlow({ flow_id: 'new', started_at: '2026-08-27T12:00:00.000Z' }));
    });
    await waitFor(() => expect(result.current.flows).toHaveLength(2));
    expect(result.current.flows[0]?.flow_id).toBe('new');
  });

  it('survives a failed initial fetch', async () => {
    const api = new ApiClient('http://127.0.0.1:8081');
    vi.spyOn(api, 'listFlows').mockRejectedValue(new Error('down'));
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    await new Promise((r) => setTimeout(r, 30));
    expect(result.current.flows).toHaveLength(0);
  });
});

describe('useFlows — filter changes', () => {
  it('replaces rather than merges when the filter changes', async () => {
    // Merging cannot remove rows, so a merge here would make every filter look
    // as though it narrowed nothing.
    const api = new ApiClient('http://127.0.0.1:8081');
    const listFlows = vi.spyOn(api, 'listFlows').mockImplementation(async (filter) => {
      const all = [
        makeFlow({ flow_id: 'a', started_at: '2026-08-27T10:00:00.000Z' }),
        makeFlow({ flow_id: 'b', started_at: '2026-08-27T11:00:00.000Z' }),
      ];
      const matched = filter?.host ? all.filter((f) => f.flow_id === 'a') : all;
      return { flows: matched, next_cursor: null, total_estimate: matched.length };
    });

    const { result, rerender } = renderHook(({ f }) => useFlows(api, f), {
      initialProps: { f: {} as Record<string, string> },
    });
    await waitFor(() => expect(result.current.flows).toHaveLength(2));

    rerender({ f: { host: 'a.example' } });
    await waitFor(() => expect(result.current.flows).toHaveLength(1));
    expect(result.current.flows[0]?.flow_id).toBe('a');
    expect(listFlows).toHaveBeenCalledTimes(2);
  });

  it('still merges on a gap refetch so live flows are not dropped', async () => {
    const api = apiWith([makeFlow({ flow_id: 'fetched' })]);
    const { result } = renderHook(() => useFlows(api, EMPTY_FILTER));
    await waitFor(() => expect(result.current.flows).toHaveLength(1));

    act(() => emit('flow.completed', makeFlow({ flow_id: 'live' })));
    await waitFor(() => expect(result.current.flows).toHaveLength(2));

    act(() => emit('stream.gap', { from_seq: 1, to_seq: 9 }));
    await new Promise((r) => setTimeout(r, 60));
    expect(result.current.flows.map((f) => f.flow_id).sort()).toEqual(['fetched', 'live']);
  });
});
