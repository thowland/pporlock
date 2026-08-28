/**
 * The popup (SPEC-3 §5.1, REQ EXT-011).
 *
 * One screen, no scrolling in the normal case. Its job is to answer three
 * questions at a glance: is the proxy on, is the daemon there, and is anything
 * altering my traffic in a way I should know about.
 */
import { useCallback, useEffect, useState } from 'react';
import { describeError } from '../shared/errors';
import type { ActionReply, StatusReply } from '../shared/messages';
import type { Message } from '../shared/messages';

async function send<T>(message: Message): Promise<T> {
  return (await chrome.runtime.sendMessage(message)) as T;
}

function hostOf(url: string | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol.startsWith('http') ? parsed.hostname : null;
  } catch {
    return null;
  }
}

export function Popup() {
  const [status, setStatus] = useState<StatusReply | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pairCode, setPairCode] = useState('');
  const [tabHost, setTabHost] = useState<string | null>(null);
  const [sessionName, setSessionName] = useState('');
  const [bypassed, setBypassed] = useState(false);

  const refresh = useCallback(async () => {
    setStatus(await send<StatusReply>({ type: 'get_status' }));
  }, []);

  useEffect(() => {
    void refresh();
    void chrome.tabs
      ?.query({ active: true, currentWindow: true })
      .then((tabs) => setTabHost(hostOf(tabs[0]?.url)));
  }, [refresh]);

  const act = useCallback(
    async (message: Message) => {
      setBusy(true);
      setError(null);
      try {
        const reply = await send<ActionReply>(message);
        if (!reply.ok) setError(reply.error ?? 'That did not work.');
        await refresh();
        return reply.ok;
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  if (!status) {
    return (
      <div className="pad">
        <h1>pporlock</h1>
        <p className="sub">Checking…</p>
      </div>
    );
  }

  const {
    state,
    daemonReachable,
    proxyControllable,
    controlLevel,
    profiles,
    counters,
    attributionGranted,
  } = status;
  const failSafeTripped = state.failSafeTrippedAt !== null;
  const devToggleActive = state.devToggles.anticache || state.devToggles.anticomp;
  const activeToggles = Object.entries(state.devToggles)
    .filter(([, on]) => on)
    .map(([name]) => name);

  // A toggle the user cannot act on must say why, not sit there greyed out.
  const toggleBlockedReason = !daemonReachable
    ? 'The daemon is not running. Start it with `pporlock run`.'
    : !state.paired
      ? 'Pair with the daemon first.'
      : !proxyControllable
        ? controlLevel === 'controlled_by_policy'
          ? 'Chrome’s proxy is controlled by an enterprise policy.'
          : 'Another extension is controlling Chrome’s proxy.'
        : null;

  return (
    <div className="pad">
      <div className="row">
        <h1>pporlock</h1>
        {daemonReachable ? (
          <span className="pill ok">
            <span className="dot" />
            daemon up
          </span>
        ) : (
          <span className="pill error">
            <span className="dot" />
            daemon down
          </span>
        )}
        <span className="spacer" />
        <button
          type="button"
          role="switch"
          aria-checked={state.proxyEnabled}
          aria-label="Proxy"
          className="toggle"
          disabled={busy || toggleBlockedReason !== null}
          onClick={() => void act({ type: 'set_proxy', enabled: !state.proxyEnabled })}
        />
      </div>

      {failSafeTripped && (
        <div className="row">
          <div className="alert error">
            <b>pporlock turned the proxy off.</b>
            The daemon stopped responding, so Chrome was returned to a direct connection — your
            browsing is working. Start it with <code>pporlock run</code>, then turn the proxy back
            on.
            <div style={{ marginTop: 6 }}>
              <button
                type="button"
                className="link"
                onClick={() => void act({ type: 'dismiss_error' })}
              >
                dismiss
              </button>
            </div>
          </div>
        </div>
      )}

      {devToggleActive && (
        <div className="row">
          <div className="alert warn">
            <b>⚠ {activeToggles.join(' + ')} active</b>
            Traffic is being altered in a way that makes normal behaviour unreproducible. Turn these
            off for ordinary browsing.
            <div style={{ marginTop: 6 }}>
              {activeToggles.map((name) => (
                <button
                  key={name}
                  type="button"
                  className="link"
                  style={{ marginRight: 10 }}
                  onClick={() =>
                    void act({
                      type: 'set_dev_toggle',
                      toggle: name as 'anticache' | 'anticomp',
                      value: false,
                    })
                  }
                >
                  turn off {name}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {state.lastError && !failSafeTripped && (
        // A recorded error is shown as what it means and what to do, never as
        // its code (REQ EXT-024). The code stays available as a tooltip for a
        // bug report.
        <div className="row">
          <div
            className={`alert ${describeError(state.lastError.code).actionable ? 'error' : 'warn'}`}
            title={state.lastError.code}
          >
            <b>{describeError(state.lastError.code).title}</b>
            {describeError(state.lastError.code).meaning}
            <div style={{ marginTop: 6 }}>{describeError(state.lastError.code).remedy}</div>
            <div style={{ marginTop: 6 }}>
              <button
                type="button"
                className="link"
                onClick={() => void act({ type: 'dismiss_error' })}
              >
                dismiss
              </button>
            </div>
          </div>
        </div>
      )}

      {toggleBlockedReason && !failSafeTripped && (
        <div className="row">
          <span className="sub">{toggleBlockedReason}</span>
        </div>
      )}

      {!state.paired && daemonReachable && (
        <>
          <hr />
          <div className="row">
            <span className="sub">
              Run <code>pporlock pair</code> and enter the code:
            </span>
          </div>
          <div className="row">
            <input
              type="text"
              aria-label="Pairing code"
              placeholder="0000-0000-0000-0000"
              value={pairCode}
              onChange={(e) => setPairCode(e.target.value)}
            />
            <button
              type="button"
              className="act"
              disabled={busy || pairCode.length === 0}
              onClick={() => void act({ type: 'pair', code: pairCode.trim() })}
            >
              pair
            </button>
          </div>
        </>
      )}

      {state.paired && (
        <>
          <hr />
          <div className="row">
            <span className="sub">profile</span>
            <select
              aria-label="Active profile"
              value={state.activeProfile ?? ''}
              disabled={busy || profiles.length === 0}
              onChange={(e) => void act({ type: 'activate_profile', name: e.target.value })}
            >
              {profiles.length === 0 && <option value="">—</option>}
              {profiles.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
        </>
      )}

      {counters && (
        <>
          <hr />
          {/* Global counts this sprint. Per-tab needs attribution (Sprint 6). */}
          <div className="stats">
            <div className="stat">
              <b>{counters.flows.toLocaleString()}</b>
              <span>flows</span>
            </div>
            <div className="stat">
              <b>{counters.blocked.toLocaleString()}</b>
              <span>blocked</span>
            </div>
            <div className="stat">
              <b>{counters.modified.toLocaleString()}</b>
              <span>modified</span>
            </div>
            <div className="stat">
              <b>{counters.passthrough.toLocaleString()}</b>
              <span>tunnel</span>
            </div>
          </div>
          <div className="row">
            {attributionGranted ? (
              <span className="sub">per-tab attribution is on</span>
            ) : (
              <>
                <span className="sub">counts are browser-wide</span>
                <span className="spacer" />
                <button
                  type="button"
                  className="link"
                  onClick={() => {
                    // Requires a user gesture, so it is requested from the click
                    // itself rather than through the service worker.
                    void chrome.permissions
                      .request({ origins: ['<all_urls>'] })
                      .then(() => refresh());
                  }}
                >
                  enable per-tab counts
                </button>
              </>
            )}
          </div>
        </>
      )}

      {state.paired && (
        <>
          <hr />
          {state.recordingSession === null ? (
            <div className="row">
              <input
                type="text"
                aria-label="Session name"
                placeholder="session name"
                value={sessionName}
                onChange={(e) => setSessionName(e.target.value)}
              />
              <button
                type="button"
                className="act"
                disabled={busy || !daemonReachable}
                onClick={() =>
                  void act({ type: 'start_recording', name: sessionName.trim() }).then((ok) => {
                    if (ok) setSessionName('');
                  })
                }
              >
                record
              </button>
            </div>
          ) : (
            // Recording is opt-in and off by default (REQ CAP-020). While it is
            // on it says so unmissably: a proxy quietly writing every flow it
            // sees to disk is a different, worse tool than this one.
            <div className="row">
              <div className="alert warn">
                <b>● recording</b>
                Flows are being written to a session on disk. Secrets are masked as they are
                written, so the file never holds the real value.
                <div style={{ marginTop: 6 }}>
                  <button
                    type="button"
                    className="link"
                    disabled={busy}
                    onClick={() => void act({ type: 'stop_recording' })}
                  >
                    stop recording
                  </button>
                </div>
              </div>
            </div>
          )}
        </>
      )}

      {tabHost && state.paired && (
        <>
          <hr />
          <div className="row">
            <span className="host" title={tabHost}>
              {tabHost}
            </span>
            <span className="spacer" />
            <button
              type="button"
              className="act"
              disabled={busy || bypassed}
              onClick={() =>
                void act({ type: 'bypass_host', host: tabHost }).then((ok) => setBypassed(!!ok))
              }
            >
              {bypassed ? 'bypassed' : 'bypass host'}
            </button>
          </div>
        </>
      )}

      {error && (
        <div className="row">
          <span className="alert error">{error}</span>
        </div>
      )}

      <hr />
      <div className="row">
        <button
          type="button"
          className="link"
          onClick={() => void chrome.tabs.create({ url: state.controlOrigin })}
        >
          open web UI
        </button>
        <span className="spacer" />
        <span className="sub">{status.version ? `v${status.version}` : ''}</span>
      </div>
    </div>
  );
}
