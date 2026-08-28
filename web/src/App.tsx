/**
 * The application shell (SPEC-2 §3).
 *
 * The status bar and the disconnected banner sit outside the router because
 * they are true of the daemon, not of a view: whichever page you are on, the
 * UI must keep telling you whether it is actually connected (REQ WUI-013).
 *
 * Traffic stays the default view. The authoring views (modules, editor, rule
 * builder, profiles) hang off the same shell and share the same API client.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiClient } from './api/client';
import type { FlowFilter, FlowRecord, Rule } from './api/types';
import { FlowDetail } from './components/detail/FlowDetail';
import { DisconnectedBanner } from './components/DisconnectedBanner';
import { FilterBar } from './components/FilterBar';
import { FlowTable } from './components/FlowTable';
import { StatusBar } from './components/StatusBar';
import { ModuleLibrary } from './components/modules/ModuleLibrary';
import { ModuleEditor } from './components/modules/ModuleEditor';
import { ProfilesView } from './components/profiles/ProfilesView';
import { CreateRuleMenu } from './components/rules/CreateRuleMenu';
import { RuleFromFlowView } from './components/rules/RuleFromFlowView';
import { useDaemonState } from './hooks/useDaemonState';
import { useFlows } from './hooks/useFlows';
import { useHashRoute, type Route } from './lib/router';

const NAV: { route: Route; label: string }[] = [
  { route: { view: 'traffic' }, label: 'Traffic' },
  { route: { view: 'modules' }, label: 'Modules' },
  { route: { view: 'profiles' }, label: 'Profiles' },
];

export function App({ api }: { api: ApiClient }) {
  const [filter, setFilter] = useState<FlowFilter>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pendingRule, setPendingRule] = useState<Rule | null>(null);
  const [route, navigate] = useHashRoute();
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
    (module: string) => navigate({ view: 'module', name: module }),
    [navigate],
  );

  /** Second click of REQ WUI-008: an intent chosen becomes a pre-filled rule. */
  const startRuleFromFlow = useCallback(
    (rule: Rule) => {
      setPendingRule(rule);
      navigate({ view: 'newrule' });
    },
    [navigate],
  );

  const traffic = (
    <>
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
            renderActions={(flow: FlowRecord) => (
              <CreateRuleMenu api={api} flow={flow} onRule={startRuleFromFlow} />
            )}
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

  const body = () => {
    switch (route.view) {
      case 'modules':
        return <ModuleLibrary api={api} onOpen={openModule} />;
      case 'module':
        return (
          <ModuleEditor api={api} name={route.name} onBack={() => navigate({ view: 'modules' })} />
        );
      case 'profiles':
        return (
          <ProfilesView api={api} activeProfile={state?.active_profile} onActivated={refresh} />
        );
      case 'newrule':
        // Deep-linking here without a rule in hand is meaningless, so it falls
        // back to the view the rule would have come from.
        return pendingRule === null ? (
          traffic
        ) : (
          <RuleFromFlowView
            api={api}
            rule={pendingRule}
            onCreated={(moduleName) => {
              setPendingRule(null);
              navigate({ view: 'module', name: moduleName });
            }}
            onCancel={() => {
              setPendingRule(null);
              navigate({ view: 'traffic' });
            }}
          />
        );
      default:
        return traffic;
    }
  };

  return (
    <>
      <StatusBar
        state={state}
        connection={connection}
        streamState={streamState}
        flowCount={flows.length}
      />
      <DisconnectedBanner connection={connection} onRetry={refresh} />
      <nav className="nav" aria-label="Views">
        {NAV.map((item) => {
          const current =
            item.route.view === route.view ||
            (item.route.view === 'modules' && route.view === 'module') ||
            (item.route.view === 'traffic' && route.view === 'newrule');
          return (
            <button
              key={item.label}
              type="button"
              className={current ? 'navlink active' : 'navlink'}
              aria-current={current ? 'page' : undefined}
              onClick={() => navigate(item.route)}
            >
              {item.label}
            </button>
          );
        })}
      </nav>
      {body()}
    </>
  );
}
