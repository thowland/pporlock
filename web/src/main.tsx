/**
 * Web UI entry point.
 *
 * The page is served same-origin by the daemon (REQ API-003), so it obtains the
 * token from a bootstrap element the server can fill rather than pairing. Until
 * that lands the token is read from the URL fragment on first load and removed
 * immediately, so it never persists in history.
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { ApiClient } from './api/client';
import './styles/app.css';

function bootstrapToken(): string | null {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="pporlock-token"]');
  if (meta?.content) return meta.content;

  const hash = window.location.hash;
  if (hash.startsWith('#token=')) {
    const token = decodeURIComponent(hash.slice('#token='.length));
    // Strip it straight away: a token in the address bar ends up in history.
    window.history.replaceState(null, '', window.location.pathname);
    try {
      sessionStorage.setItem('pporlock.token', token);
    } catch {
      /* private browsing; the in-memory client still has it */
    }
    return token;
  }

  try {
    return sessionStorage.getItem('pporlock.token');
  } catch {
    return null;
  }
}

const api = new ApiClient(window.location.origin);
api.setToken(bootstrapToken());

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App api={api} />
    </StrictMode>,
  );
}
