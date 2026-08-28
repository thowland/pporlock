/**
 * The application shell (SPEC-2 §3).
 *
 * Sprint 4 is the visual MVP: one view, the live traffic table. The remaining
 * views (modules, profiles, sessions, settings) land in their own sprints and
 * hang off this shell.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiClient } from './api/client';
import type { FlowFilter, FlowRecord } from './api/types';
import { FlowDetail } from './components/detail/FlowDetail';
import { DisconnectedBanner } from './components/DisconnectedBanner';
import { FilterBar } from './components/FilterBar';
import { FlowTable } from './components/FlowTable';
import { StatusBar } from './components/StatusBar';
import { useDaemonState } from './hooks/useDaemonState';
import { useFlows } from './hooks/useFlows';

export function App({ api }: { api: ApiClient }) {
  const [filter, setFilter] = useState<FlowFilter>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { state, connection, refresh } = useDaemonState(api);
  const { flows, streamState, paused, heldCount, setPaused, clear } = useFlows(api, filter);

  const hasFilter = useMemo(() => Object.keys(filter).length > 0, [filter]);
  const selected = useMemo(
    () => flows.find((f) => f.flow_id === selectedId) ?? null,
    [flows, selectedId],
  );

  // Escape closes the panel: the table is the primary surface and should always
  // be one keystroke away.
  useEffect(() => {
    if (selectedId === null) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelectedId(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selectedId]);

  const openModule = useCallback(
    (module: string) => {
      // The module library lands in Sprint 12; until then this is where the link
      // will point rather than a dead control.
      window.open(`${api.origin}/#/modules/${encodeURIComponent(module)}`, '_blank');
    },
    [api.origin],
  );

  return (
    <>
      <StatusBar
        state={state}
        connection={connection}
        streamState={streamState}
        flowCount={flows.length}
      />
      <DisconnectedBanner connection={connection} onRetry={refresh} />
      <FilterBar
        filter={filter}
        onChange={setFilter}
        paused={paused}
        heldCount={heldCount}
        onTogglePause={() => setPaused(!paused)}
        onClear={clear}
      />
      <div className="layout">
        <div className="tablewrap">
          <FlowTable
            flows={flows}
            connected={connection === 'connected'}
            hasFilter={hasFilter}
            selectedId={selectedId}
            onSelect={(flow: FlowRecord) => setSelectedId(flow.flow_id)}
          />
        </div>
        {selected && (
          <FlowDetail
            flow={selected}
            api={api}
            onClose={() => setSelectedId(null)}
            onOpenModule={openModule}
          />
        )}
      </div>
    </>
  );
}
