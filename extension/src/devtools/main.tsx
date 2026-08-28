/**
 * Panel entry point.
 *
 * The panel obtains the daemon origin and token from the service worker rather
 * than holding its own: there is one paired token per install, and duplicating
 * it here would mean two places to get wrong.
 */
import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ControlApi } from '../shared/api';
import type { StatusReply } from '../shared/messages';
import { PanelView } from './PanelView';
import './panel.css';

function Root() {
  const [api, setApi] = useState<ControlApi | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    void chrome.runtime
      .sendMessage({ type: 'get_status' })
      .then((reply) => {
        const status = reply as StatusReply;
        if (!status.state.paired || !status.state.token) {
          setProblem('Not paired. Run `pporlock pair` and enter the code in the popup.');
          return;
        }
        const client = new ControlApi(status.state.controlOrigin);
        client.setToken(status.state.token);
        setApi(client);
      })
      .catch((err: unknown) => setProblem(String(err)));
  }, []);

  if (problem) return <div className="err">{problem}</div>;
  if (!api)
    return (
      <p className="dim" style={{ padding: 10 }}>
        Connecting…
      </p>
    );

  return (
    <PanelView
      api={api}
      tabId={chrome.devtools.inspectedWindow.tabId}
      onOpenModule={(module) =>
        // Authoring belongs in the web UI; the panel links rather than embeds
        // (SPEC-3 §7.3).
        chrome.tabs.create({ url: `${api.origin}/#/modules/${encodeURIComponent(module)}` })
      }
    />
  );
}

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <Root />
    </StrictMode>,
  );
}
