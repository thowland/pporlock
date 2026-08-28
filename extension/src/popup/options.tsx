/**
 * Options page (SPEC-3 §9).
 *
 * Everything that is set once and then forgotten: pairing, where the daemon is,
 * how warnings behave, and the diagnostics you need when attribution or the
 * fail-safe misbehaves.
 */
import { StrictMode, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { describeError } from '../shared/errors';
import type { ActionReply, Message, StatusReply } from '../shared/messages';
import './popup.css';

async function send<T>(message: Message | { type: string; [k: string]: unknown }): Promise<T> {
  return (await chrome.runtime.sendMessage(message)) as T;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <>
      <hr />
      <div className="row">
        <b>{title}</b>
      </div>
      {children}
    </>
  );
}

function Options() {
  const [status, setStatus] = useState<StatusReply | null>(null);
  const [code, setCode] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [granted, setGranted] = useState(false);

  const refresh = useCallback(() => {
    void send<StatusReply>({ type: 'get_status' }).then(setStatus);
    void chrome.permissions
      ?.contains({ origins: ['<all_urls>'] })
      .then(setGranted)
      .catch(() => setGranted(false));
  }, []);

  useEffect(refresh, [refresh]);

  const act = useCallback(
    async (msg: Message | { type: string; [k: string]: unknown }, note?: string) => {
      const reply = await send<ActionReply>(msg);
      setMessage(reply.ok ? (note ?? 'Done.') : (reply.error ?? 'That did not work.'));
      refresh();
    },
    [refresh],
  );

  if (!status) {
    return (
      <div className="pad" style={{ width: 460 }}>
        <h1>pporlock options</h1>
        <p className="sub">Loading…</p>
      </div>
    );
  }

  const { state } = status;

  return (
    <div className="pad" style={{ width: 460 }}>
      <h1>pporlock options</h1>

      <Section title="Connection">
        <div className="row">
          <span className="sub">daemon</span>
          <span className="host">{state.controlOrigin}</span>
          <span className="spacer" />
          <span className={`pill ${status.daemonReachable ? 'ok' : 'error'}`}>
            <span className="dot" />
            {status.daemonReachable ? 'reachable' : 'unreachable'}
          </span>
        </div>
        <div className="row">
          <span className="sub">chrome proxy</span>
          <span className="host">{status.controlLevel}</span>
        </div>
      </Section>

      <Section title="Pairing">
        <div className="row">
          <span className="sub">
            {state.paired
              ? 'Paired. The token is held by the extension and never read from disk.'
              : 'Run `pporlock pair` and enter the code below.'}
          </span>
        </div>
        {!state.paired && (
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
              onClick={() => void act({ type: 'pair', code: code.trim() }, 'Paired.')}
            >
              pair
            </button>
          </div>
        )}
      </Section>

      <Section title="Scope">
        <div className="row">
          <label className="sub">
            <input
              type="radio"
              name="scope"
              checked={state.proxyScope === 'all'}
              onChange={() => void act({ type: 'set_proxy_scope', scope: 'all' })}
            />{' '}
            Proxy all traffic
          </label>
        </div>
        <div className="row">
          <label className="sub">
            <input
              type="radio"
              name="scope"
              checked={state.proxyScope === 'scoped'}
              onChange={() => void act({ type: 'set_proxy_scope', scope: 'scoped' })}
            />{' '}
            Proxy only these hosts
          </label>
        </div>
        {state.proxyScope === 'scoped' && (
          <>
            <div className="row">
              <textarea
                aria-label="Scoped hosts"
                rows={4}
                defaultValue={state.scopedHosts.join('\n')}
                onBlur={(e) =>
                  void act(
                    {
                      type: 'set_proxy_scope',
                      scope: 'scoped',
                      hosts: e.target.value
                        .split('\n')
                        .map((h) => h.trim())
                        .filter(Boolean),
                    },
                    'Scope updated.',
                  )
                }
              />
            </div>
            <div className="row">
              <span className="sub">
                One host pattern per line — <code>*.example.com</code> works. An empty list in
                scoped mode proxies everything, which is the safe reading of &ldquo;nothing
                selected&rdquo;.
              </span>
            </div>
          </>
        )}
      </Section>

      <Section title="Warnings">
        <div className="row">
          <label className="sub">
            <input
              type="checkbox"
              checked={state.bannerEnabled}
              onChange={(e) => void act({ type: 'set_banner_enabled', enabled: e.target.checked })}
            />{' '}
            Warn in the page when pporlock modifies it
          </label>
        </div>
        <div className="row">
          <span className="sub">
            Suppressing a host silences the banner, not the fact — the badge and the DevTools panel
            still report it.
          </span>
        </div>
        {state.suppressedHosts.length === 0 ? (
          <div className="row">
            <span className="sub">No hosts suppressed.</span>
          </div>
        ) : (
          state.suppressedHosts.map((host) => (
            <div className="row" key={host}>
              <span className="host">{host}</span>
              <span className="spacer" />
              <button
                type="button"
                className="link"
                onClick={() => void act({ type: 'unsuppress_host', host }, 'Removed.')}
              >
                remove
              </button>
            </div>
          ))
        )}
      </Section>

      <Section title="Per-tab attribution">
        <div className="row">
          <span className="sub">
            {granted
              ? 'Granted. Counts and the DevTools panel are scoped to each tab.'
              : 'Not granted. Counts are browser-wide.'}
          </span>
        </div>
        <div className="row">
          <span className="sub">
            Attribution needs host access so Chrome will report which tab made a request. Everything
            else works without it.
          </span>
        </div>
        {!granted && (
          <div className="row">
            <button
              type="button"
              className="act"
              onClick={() => {
                void chrome.permissions.request({ origins: ['<all_urls>'] }).then(() => refresh());
              }}
            >
              enable per-tab attribution
            </button>
          </div>
        )}
      </Section>

      <Section title="Diagnostics">
        {state.lastError ? (
          <div className="row">
            <div className="alert warn" title={state.lastError.code}>
              <b>{describeError(state.lastError.code).title}</b>
              {describeError(state.lastError.code).meaning}
              <div style={{ marginTop: 6 }}>{describeError(state.lastError.code).remedy}</div>
            </div>
          </div>
        ) : (
          <div className="row">
            <span className="sub">last error</span>
            <span className="host">none</span>
          </div>
        )}
        <div className="row">
          <span className="sub">fail-safe</span>
          <span className="host">
            {state.failSafeTrippedAt
              ? `tripped ${new Date(state.failSafeTrippedAt).toLocaleString()}`
              : 'not tripped'}
          </span>
        </div>
        <div className="row">
          <span className="sub">modules</span>
          <span className="host">
            {state.moduleHealth.errors} error(s), {state.moduleHealth.quarantined} quarantined
          </span>
        </div>
      </Section>

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
