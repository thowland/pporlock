/**
 * Help (REQ DOC-*, WUI-015).
 *
 * The UI is dense on purpose — it is a tool that sits beside devtools — and
 * dense interfaces need somewhere that says what the density means. Two things
 * live here that live nowhere else in the product:
 *
 *   1. A walk through what each view shows, in the order a new user meets them.
 *      Every other explanation in this UI is a tooltip on a control someone has
 *      already found; this is for the parts they have not.
 *
 *   2. The Chrome extension's error states, in full. The popup explains each
 *      one at the moment it happens, and then it is gone — a 330px surface that
 *      closes when you look away. Anyone trying to work out what they saw
 *      yesterday has had nowhere to look. The list is held to the extension's
 *      own error union by `extension-errors.test.ts`.
 *
 * It is a route rather than a dialog because it is long, deep-linkable, and
 * something the extension links into from outside the app (`#/help`).
 */
import { docUrl, HELP_DOCS, HOMEPAGE, LAMP_COLORS } from '../../lib/about';
import { EXTENSION_ERRORS } from '../../lib/extension-errors';
import { Ticked } from './Ticked';

interface Section {
  id: string;
  title: string;
}

const SECTIONS: Section[] = [
  { id: 'help-start', title: 'Getting a first flow' },
  { id: 'help-views', title: 'What each view shows' },
  { id: 'help-extension', title: 'The Chrome extension' },
  { id: 'help-errors', title: 'Extension error states' },
  { id: 'help-docs', title: 'Further reading' },
];

function Lamp({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <li className="lamp-row">
      <span className="lamp" style={{ background: color }} aria-hidden="true" />
      <span>{children}</span>
    </li>
  );
}

export function HelpView({ onAbout }: { onAbout: () => void }) {
  return (
    <div className="helpview">
      <div className="viewbar">
        <h2>Help</h2>
        <span className="spacer" style={{ flex: 1 }} />
        <button type="button" className="action" onClick={onAbout}>
          About pporlock
        </button>
      </div>

      <nav className="help-toc" aria-label="Help contents">
        <ul>
          {SECTIONS.map((section) => (
            <li key={section.id}>
              {/* A button, not an anchor: an in-page `#anchor` href would
                  replace the route hash and navigate away from this view. */}
              <button
                type="button"
                className="linkish"
                onClick={() => document.getElementById(section.id)?.scrollIntoView()}
              >
                {section.title}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <section className="help-section" id="help-start">
        <h3>Getting a first flow</h3>
        <p>
          Three things have to be true before anything appears in the traffic table, and they fail
          independently:
        </p>
        <ol>
          <li>
            The daemon is running. The bar at the top of this page says so — if it says
            disconnected, this page is showing you the last thing it knew.
          </li>
          <li>
            The extension is paired. Run <code>pporlock pair</code> and type the code into the
            extension popup. Without this the proxy still works, but counts, profiles and recording
            do not.
          </li>
          <li>
            The proxy toggle in the popup is on, and the lamp on the toolbar icon is green. Green
            means traffic really is going through pporlock; grey means it is not, whatever the
            toggle says.
          </li>
        </ol>
        <p>
          Then browse. If the table stays empty, the host is probably in the exclusion list —
          pporlock ships with 33 hosts it will not decrypt, including OS updates, certificate
          revocation and banking. Settings shows the list.
        </p>
      </section>

      <section className="help-section" id="help-views">
        <h3>What each view shows</h3>

        <h4>Traffic</h4>
        <p>
          Every flow the proxy has seen, newest first, live. The flags column is the short answer to
          "was this touched": blocked, modified, a module error, or nothing at all. Pause holds the
          stream without dropping anything — the held count tells you how much is waiting — and the
          filter bar narrows by host, method, status and outcome.
        </p>
        <p>
          A tunnelled host shows as a passthrough row with no body: pporlock saw the connection and
          deliberately did not decrypt it. That is what an exclusion looks like from here.
        </p>

        <h4>Flow detail, and provenance</h4>
        <p>
          Selecting a row opens the request and response beside the table. The part worth
          understanding is <b>provenance</b>: a structural record, carried by every flow, of which
          modules looked at it, which rules matched, what each one changed, and why anything that
          did not run did not run. It is not a log — it is a return value, so it cannot be missing.
          When a page breaks, this is the view that says which of your modules did it.
        </p>
        <p>
          Secrets are masked. Cookies, <code>Authorization</code> headers, credential-shaped JSON
          keys and query-string secrets appear as <code>«redacted:sha1=…,len=…»</code>. Masking
          happens at the moment of writing, so a recorded session on disk never held the real value.
          Live flows only can be unmasked, one value at a time, from this panel — never in a
          session, and never from the MCP interface.
        </p>

        <h4>Modules</h4>
        <p>
          Your module library: what is loaded, what is enabled, what failed and why. A load error or
          a quarantine renders expanded, with the traceback, because that is what you came to the
          page to read. Priority is what the daemon evaluates, so the reorder arrows write priority
          back rather than remembering a list position. "How to write a module" opens the guides.
        </p>

        <h4>Profiles</h4>
        <p>
          A named set of enabled modules and extra exclusions, so a debugging setup can be switched
          on and off in one action instead of six toggles.
        </p>

        <h4>Sessions</h4>
        <p>
          Recording is off by default and never starts on its own. A session is a recording of flows
          on disk, redacted as it was written. From a session you can browse what happened, or
          <b> dry-run</b> a module against it — which replays the recorded flows through your rules
          and shows the diffs without touching live browsing. This is how a module gets tested
          before it is allowed near real traffic.
        </p>
        <p className="help-note">
          Dry run executes module Python. It is safe for your browsing, not safe from the module.
        </p>

        <h4>Settings</h4>
        <p>
          Where the daemon is listening, the dev toggles, and the exclusion list. The dev toggles
          (anticache, anticomp) make ordinary behaviour unreproducible and say so loudly wherever
          they are on; turn them off before concluding anything about a site.
        </p>
      </section>

      <section className="help-section" id="help-extension">
        <h3>The Chrome extension</h3>
        <p>
          The extension does three jobs: it points Chrome at the proxy, it reports what the daemon
          is doing, and it turns the proxy off if the daemon stops answering — so a crashed daemon
          costs you a few seconds of browsing rather than all of it. The proxy does not re-arm
          itself afterwards; that is deliberate.
        </p>

        <h4>The toolbar icon</h4>
        <p>
          Two lamps are drawn into the icon. They report state; the number badge reports events.
        </p>
        <ul className="lamps">
          <Lamp color={LAMP_COLORS.proxyOn}>
            <b>Green, top left.</b> Your traffic is going through pporlock.
          </Lamp>
          <Lamp color={LAMP_COLORS.proxyOff}>
            <b>Grey, top left.</b> It is not. The proxy is off, the daemon is unreachable, the
            fail-safe tripped, or another extension holds Chrome’s proxy setting. Your browsing is
            going direct and nothing is being intercepted.
          </Lamp>
          <Lamp color={LAMP_COLORS.recording}>
            <b>Red, top right.</b> Flows are being written to a session on disk. It appears only
            while recording.
          </Lamp>
        </ul>

        <h4>Per-tab counts</h4>
        <p>
          Counts are browser-wide until you grant the extension broad host access, which Chrome
          requires before it will tell any extension which tab a request came from. It is optional
          and not taken at install. Without it, flows simply carry no tab — everything else works.
        </p>
      </section>

      <section className="help-section" id="help-errors">
        <h3>Extension error states</h3>
        <p>
          Everything the extension can report, what it means, and what to do. The code in the first
          column is the one the popup shows as a tooltip; quote it in a bug report.
        </p>
        <table className="help-errors">
          <thead>
            <tr>
              <th>Code</th>
              <th>What it means</th>
              <th>What to do</th>
            </tr>
          </thead>
          <tbody>
            {EXTENSION_ERRORS.map((error) => (
              <tr key={error.code}>
                <td>
                  <code>{error.code}</code>
                </td>
                <td>
                  <b>{error.title}</b>
                  <p>
                    <Ticked text={error.cause} />
                  </p>
                </td>
                <td>
                  <ol>
                    {error.fix.map((step) => (
                      <li key={step}>
                        <Ticked text={step} />
                      </li>
                    ))}
                  </ol>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="help-section" id="help-docs">
        <h3>Further reading</h3>
        <ul className="guides">
          {HELP_DOCS.map((doc) => (
            <li key={doc.file}>
              <a href={docUrl(doc.file)} target="_blank" rel="noreferrer">
                {doc.title}
              </a>
              <p>{doc.blurb}</p>
              <code>{doc.file}</code>
            </li>
          ))}
        </ul>
        <p>
          Everything else — the specs, the requirements, the open issues — is at{' '}
          <a href={HOMEPAGE} target="_blank" rel="noreferrer">
            {HOMEPAGE}
          </a>
          , and in your own checkout.
        </p>
      </section>
    </div>
  );
}
