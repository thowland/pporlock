/**
 * About (`#/about`).
 *
 * A route rather than a dialog because the extension links straight into it
 * from outside the app, and because "which version am I running" is the first
 * question of every diagnosis this project has ever had — it needs to be
 * something you can send someone a link to.
 *
 * Both versions are shown, and the mitmproxy one with them. The daemon and the
 * extension are installed separately and drift routinely (OI-24); mitmproxy is
 * here because it is the component whose version actually changes behaviour
 * underneath us, and `addon/normalize.py` exists precisely to absorb that.
 */
import type { DaemonState } from '../../api/types';
import {
  COPYRIGHT,
  HOMEPAGE,
  ISSUES,
  LICENSE,
  LICENSE_URL,
  PROJECT_NAME,
  SUMMARY,
  TRUST_NOTE,
} from '../../lib/about';

export function AboutView({
  state,
  onHelp,
}: {
  /** null while the daemon has not answered, or is not there. */
  state: DaemonState | null;
  onHelp: () => void;
}) {
  return (
    <div className="aboutview">
      <div className="viewbar">
        <h2>About</h2>
        <span className="spacer" style={{ flex: 1 }} />
        <button type="button" className="action" onClick={onHelp}>
          Help
        </button>
      </div>

      <section className="help-section">
        <h3>{PROJECT_NAME}</h3>
        <p>{SUMMARY}</p>

        <dl className="about-versions">
          <div>
            <dt>pporlock</dt>
            <dd>{state?.version ?? 'not connected'}</dd>
          </div>
          <div>
            <dt>mitmproxy</dt>
            <dd>{state?.mitmproxy_version ?? '—'}</dd>
          </div>
          <div>
            <dt>proxy</dt>
            <dd>{state?.proxy.running === true ? state.proxy.listen : 'not running'}</dd>
          </div>
        </dl>
        <p className="help-note">
          The Chrome extension carries its own version, shown in its popup and its about page. They
          are installed separately, so a mismatch is ordinary rather than alarming — but it is the
          first thing to check when the extension and this page disagree.
        </p>
      </section>

      <section className="help-section">
        <h3>What it does to your machine</h3>
        <p>
          pporlock terminates TLS with a certificate authority it generated and you installed. While
          the proxy is on it can read and rewrite anything your browser sends or receives, except
          the hosts in the exclusion list — which is why that list ships with OS updates,
          certificate revocation and banking already in it.
        </p>
        <p>
          Everything binds to loopback and is rejected at startup if configured otherwise. Nothing
          is sent anywhere. Recorded sessions are redacted as they are written, so a session file on
          disk never holds a real cookie or credential.
        </p>
        <div className="banner warn" role="note">
          {TRUST_NOTE}
        </div>
      </section>

      <section className="help-section">
        <h3>Source and licence</h3>
        <ul className="guides">
          <li>
            <a href={HOMEPAGE} target="_blank" rel="noreferrer">
              github.com/thowland/pporlock
            </a>
            <p>Source, documentation, specifications and the open-issue list.</p>
          </li>
          <li>
            <a href={ISSUES} target="_blank" rel="noreferrer">
              Report a problem
            </a>
            <p>
              Include both version numbers and, if the extension is involved, the error code from
              its popup tooltip.
            </p>
          </li>
        </ul>
        <p>
          {COPYRIGHT}. Released under the{' '}
          <a href={LICENSE_URL} target="_blank" rel="noreferrer">
            {LICENSE}
          </a>
          . pporlock is free software: you may redistribute and modify it under those terms. It
          comes with absolutely no warranty.
        </p>
      </section>
    </div>
  );
}
