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
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiClient } from './api/client';
import type { FlowFilter, FlowRecord, Rule } from './api/types';
import { FlowDetail } from './components/detail/FlowDetail';
import { DisconnectedBanner } from './components/DisconnectedBanner';
import { ExcludeHostAction } from './components/exclusions/ExcludeHostAction';
import { FilterBar } from './components/FilterBar';
import { FlowTable } from './components/FlowTable';
import { StatusBar } from './components/StatusBar';
import { ModuleLibrary } from './components/modules/ModuleLibrary';
import { ModuleEditor } from './components/modules/ModuleEditor';
import { ProfilesView } from './components/profiles/ProfilesView';
import { CreateRuleMenu } from './components/rules/CreateRuleMenu';
import { RuleFromFlowView } from './components/rules/RuleFromFlowView';
import { SessionsView } from './components/sessions/SessionsView';
import { SessionBrowser } from './components/sessions/SessionBrowser';
import { DryRunView } from './components/sessions/DryRunView';
import { SettingsView } from './components/settings/SettingsView';
import { useDaemonState } from './hooks/useDaemonState';
import { useFlows } from './hooks/useFlows';
import { useHashRoute, type Route } from './lib/router';

const NAV: { route: Route; label: string }[] = [
  { route: { view: 'traffic' }, label: 'Traffic' },
  { route: { view: 'modules' }, label: 'Modules' },
  { route: { view: 'profiles' }, label: 'Profiles' },
  { route: { view: 'sessions' }, label: 'Sessions' },
  { route: { view: 'settings' }, label: 'Settings' },
];

/** Which nav item is highlighted for a route that has no nav item of its own. */
const NAV_GROUP: Record<Route['view'], Route['view']> = {
  traffic: 'traffic',
  newrule: 'traffic',
  modules: 'modules',
  module: 'modules',
  profiles: 'profiles',
  sessions: 'sessions',
  session: 'sessions',
  dryrun: 'sessions',
  settings: 'settings',
};

export function App({ api }: { api: ApiClient }) {
  const [filter, setFilter] = useState<FlowFilter>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pendingRule, setPendingRule] = useState<Rule | null>(null);
  const [route, navigate] = useHashRoute();
  const { state, connection, refresh } = useDaemonState(api);
  const { flows, streamState, paused, heldCount, setPaused, clear } = useFlows(api, filter);

  const main = useRef<HTMLElement | null>(null);
  const lastRow = useRef<HTMLElement | null>(null);

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

  // Focus moves to the new view on a route change (REQ WUI-015). Without this a
  // keyboard or screen-reader user stays parked on the nav button they pressed
  // while the entire page beneath them changed — the classic SPA failure.
  useEffect(() => {
    main.current?.focus();
  }, [route.view]);

  /** Closing the detail panel returns focus to the row that opened it. */
  const closeDetail = useCallback(() => {
    setSelectedId(null);
    lastRow.current?.focus();
  }, []);

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
            onSelect={(flow: FlowRecord) => {
              // Remember where focus was so closing the panel can put it back.
              lastRow.current = document.activeElement as HTMLElement | null;
              setSelectedId(flow.flow_id);
            }}
            renderActions={(flow: FlowRecord) => (
              <>
                <CreateRuleMenu api={api} flow={flow} onRule={startRuleFromFlow} />
                {/* One click from the row to a tunnelled host (REQ PXY-016).
                    A passthrough flow already carries its host in a different
                    place, and excluding an already-excluded host is answered
                    rather than duplicated. */}
                <ExcludeHostAction
                  api={api}
                  host={flow.request?.host ?? flow.passthrough?.host}
                  surface="flow table"
                />
              </>
            )}
          />
        </div>
        {selected && (
          <FlowDetail
            flow={selected}
            api={api}
            onClose={closeDetail}
            onOpenModule={openModule}
            // Live flows only (REQ CAP-043). The session browser passes no
            // equivalent, which is what makes the control absent there.
            onUnmask={(fieldPath) =>
              api.unmask(selected.flow_id, fieldPath).then((result) => result.value)
            }
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
      case 'sessions':
        return (
          <SessionsView
            api={api}
            onOpen={(id) => navigate({ view: 'session', id })}
            onDryRun={(id) => navigate({ view: 'dryrun', id })}
            onChanged={refresh}
          />
        );
      case 'session':
        return (
          <SessionBrowser
            api={api}
            sessionId={route.id}
            onBack={() => navigate({ view: 'sessions' })}
            onDryRun={(id) => navigate({ view: 'dryrun', id })}
            onOpenModule={openModule}
          />
        );
      case 'dryrun':
        return (
          <DryRunView
            api={api}
            sessionId={route.id}
            onBack={() => navigate({ view: 'sessions' })}
            onOpenModule={openModule}
          />
        );
      case 'settings':
        return <SettingsView api={api} />;
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
          const current = item.route.view === NAV_GROUP[route.view];
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
      {/* A real landmark, and the focus target on route change. `tabIndex={-1}`
          makes it programmatically focusable without adding a tab stop. */}
      <main className="mainview" ref={main} tabIndex={-1} aria-label={route.view}>
        {body()}
      </main>
    </>
  );
}
