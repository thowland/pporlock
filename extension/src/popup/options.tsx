/**
 * Options page.
 *
 * Sprint 5 provides pairing and connection settings — the two things a user may
 * need before the popup is usable at all. The full surface (warnings,
 * suppression list, badge preferences, attribution diagnostics) lands in
 * Sprint 15.
 */
import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { ActionReply, StatusReply } from '../shared/messages';
import './popup.css';

function Options() {
  const [status, setStatus] = useState<StatusReply | null>(null);
  const [code, setCode] = useState('');
  const [message, setMessage] = useState<string | null>(null);

  const refresh = () =>
    void chrome.runtime
      .sendMessage({ type: 'get_status' })
      .then((reply) => setStatus(reply as StatusReply));

  useEffect(refresh, []);

  return (
    <div className="pad" style={{ width: 420 }}>
      <h1>pporlock options</h1>
      <hr />
      <div className="row">
        <span className="sub">control origin</span>
        <span className="host">{status?.state.controlOrigin ?? '—'}</span>
      </div>
      <div className="row">
        <span className="sub">paired</span>
        <span>{status?.state.paired ? 'yes' : 'no'}</span>
      </div>
      <hr />
      <div className="row">
        <span className="sub">
          Pairing: run <code>pporlock pair</code>, then enter the code here.
        </span>
      </div>
      <div className="row">
        <input
          type="text"
          aria-label="Pairing code"
          value={code}
          onChange={(e) => setCode(e.target.value)}
        />
        <button
          type="button"
          className="act"
          onClick={() =>
            void chrome.runtime.sendMessage({ type: 'pair', code: code.trim() }).then((reply) => {
              const result = reply as ActionReply;
              setMessage(result.ok ? 'Paired.' : (result.error ?? 'Pairing failed.'));
              refresh();
            })
          }
        >
          pair
        </button>
      </div>
      {message && (
        <div className="row">
          <span className="sub">{message}</span>
        </div>
      )}
    </div>
  );
}

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <Options />
    </StrictMode>,
  );
}
