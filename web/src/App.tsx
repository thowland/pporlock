/**
 * The application shell (SPEC-2 §3).
 *
 * Sprint 4 is the visual MVP: one view, the live traffic table. The remaining
 * views (modules, profiles, sessions, settings) land in their own sprints and
 * hang off this shell.
 */
import { useMemo, useState } from 'react';
import { ApiClient } from './api/client';
import type { FlowFilter } from './api/types';
import { DisconnectedBanner } from './components/DisconnectedBanner';
import { FilterBar } from './components/FilterBar';
import { FlowTable } from './components/FlowTable';
import { StatusBar } from './components/StatusBar';
import { useDaemonState } from './hooks/useDaemonState';
import { useFlows } from './hooks/useFlows';

export function App({ api }: { api: ApiClient }) {
  const [filter, setFilter] = useState<FlowFilter>({});
  const { state, connection, refresh } = useDaemonState(api);
  const { flows, streamState, paused, heldCount, setPaused, clear } = useFlows(api, filter);

  const hasFilter = useMemo(() => Object.keys(filter).length > 0, [filter]);

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
      <div className="tablewrap">
        <FlowTable flows={flows} connected={connection === 'connected'} hasFilter={hasFilter} />
      </div>
    </>
  );
}
