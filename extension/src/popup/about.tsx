/**
 * The about page's entry point.
 *
 * Asks the service worker for status so the daemon version and the configured
 * control origin are the real ones, and renders regardless if it cannot: an
 * about box that refuses to appear because the daemon is down is useless
 * precisely when someone is trying to find out what they are running.
 */
import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { AboutView } from './AboutView';
import type { StatusReply } from '../shared/messages';
import { DEFAULT_CONTROL_ORIGIN } from '../shared/state';
import './popup.css';

function AboutPage() {
  const [status, setStatus] = useState<StatusReply | null>(null);

  useEffect(() => {
    chrome.runtime
      .sendMessage({ type: 'get_status' })
      .then((reply) => setStatus(reply as StatusReply))
      .catch(() => setStatus(null));
  }, []);

  const manifest = chrome.runtime.getManifest();
  return (
    <AboutView
      extensionVersion={status?.extensionVersion ?? manifest.version_name ?? manifest.version}
      daemonVersion={status?.version ?? null}
      controlOrigin={status?.state.controlOrigin ?? DEFAULT_CONTROL_ORIGIN}
    />
  );
}

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <AboutPage />
    </StrictMode>,
  );
}
