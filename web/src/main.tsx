/**
 * Web UI entry point.
 *
 * Sprint 0 renders only enough to prove the toolchain builds and the loopback
 * guard is wired. The shell, status bar, and flow table land in Sprint 4
 * (SPEC-2 §3, §5).
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { resolveControlOrigin } from './lib/control-origin';

function App() {
  const origin = resolveControlOrigin(window.location.origin);
  return (
    <main>
      <h1>pporlock</h1>
      <p>Control origin: {origin}</p>
    </main>
  );
}

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
