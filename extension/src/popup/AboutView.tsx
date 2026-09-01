/**
 * The about box (REQ EXT-011).
 *
 * Opened from the popup, in a tab of its own rather than inside the 330px
 * popup: the popup is a control surface that must stay one glance tall, and
 * pushing licence text into it would cost the thing it is for.
 *
 * Four jobs, in the order someone actually wants them:
 *
 *   1. what this is, including that it decrypts their traffic
 *   2. what the two lamps on the toolbar icon mean — the only place that is
 *      written down, and the icon is otherwise a puzzle
 *   3. where the documentation is, both in the web UI and on GitHub
 *   4. copyright and licence
 *
 * The web UI links are built from the control origin the extension is actually
 * configured with, not from a constant: the control port is configurable, and a
 * help link pointing at 8081 for someone running on 9000 is worse than no link.
 *
 * Named `AboutView` rather than `About` because the entry point beside it is
 * `about.tsx`, and macOS would treat the two as one file.
 */
import {
  COPYRIGHT,
  HOMEPAGE,
  ISSUES,
  LICENSE,
  LICENSE_URL,
  PROJECT_NAME,
  SUMMARY,
  TRUST_NOTE,
  WEB_UI_ABOUT,
  WEB_UI_HELP,
} from '../shared/about';
import { LIGHT_COLORS } from '../background/icon';

export interface AboutProps {
  /** The extension's own version, with any prerelease suffix (OI-25). */
  extensionVersion: string;
  /** The daemon's version, or null when it could not be asked. */
  daemonVersion: string | null;
  /** Where the web UI is being served, e.g. `http://127.0.0.1:8081`. */
  controlOrigin: string;
}

function Lamp({ color, label }: { color: string; label: string }) {
  return (
    <li className="lamp-row">
      <span className="lamp" style={{ background: color }} aria-hidden="true" />
      <span>{label}</span>
    </li>
  );
}

export function AboutView({ extensionVersion, daemonVersion, controlOrigin }: AboutProps) {
  return (
    <div className="pad about">
      <h1>{PROJECT_NAME}</h1>
      <p className="sub">
        extension {extensionVersion}
        {daemonVersion === null ? ' · daemon not reachable' : ` · daemon ${daemonVersion}`}
      </p>

      <p>{SUMMARY}</p>

      <div className="alert warn">
        <b>Modules are trusted code</b>
        {TRUST_NOTE}
      </div>

      <hr />
      <h2>The toolbar icon</h2>
      <p className="sub">
        Two lamps are drawn into the icon. They report what is true right now; the number badge
        beneath them reports what has happened.
      </p>
      <ul className="lamps">
        <Lamp
          color={LIGHT_COLORS.proxyOn}
          label="green, top left — your traffic is going through pporlock"
        />
        <Lamp
          color={LIGHT_COLORS.proxyOff}
          label="grey, top left — it is not, whatever the toggle says; your browsing is going direct"
        />
        <Lamp
          color={LIGHT_COLORS.recording}
          label="red, top right — flows are being written to a session on disk"
        />
      </ul>

      <hr />
      <h2>Documentation</h2>
      <p className="sub">
        The web UI carries the full help, including what every part of the interface means and how
        to clear each error this extension can report.
      </p>
      <ul className="links">
        <li>
          <a href={`${controlOrigin}${WEB_UI_HELP}`} target="_blank" rel="noreferrer">
            Help — using pporlock
          </a>
        </li>
        <li>
          <a href={`${controlOrigin}${WEB_UI_ABOUT}`} target="_blank" rel="noreferrer">
            About, in the web UI
          </a>
        </li>
        <li>
          <a href={HOMEPAGE} target="_blank" rel="noreferrer">
            Source, guides and releases on GitHub
          </a>
        </li>
        <li>
          <a href={ISSUES} target="_blank" rel="noreferrer">
            Report a problem
          </a>
        </li>
      </ul>
      <p className="sub">
        The first two need the daemon running at <code>{controlOrigin}</code>.
      </p>

      <hr />
      <h2>Licence</h2>
      <p className="sub">
        {COPYRIGHT}. Released under the{' '}
        <a href={LICENSE_URL} target="_blank" rel="noreferrer">
          {LICENSE}
        </a>
        . This program comes with absolutely no warranty; it is free software, and you are welcome
        to redistribute it under those terms.
      </p>
    </div>
  );
}
