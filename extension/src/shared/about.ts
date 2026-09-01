/**
 * Who this is and where it came from.
 *
 * Constants rather than literals scattered through JSX because three surfaces
 * quote them — the about page, the popup, and the web UI's own about view — and
 * a project URL that is right in two of three is the kind of wrong nobody
 * notices until someone tries to file a bug.
 */

export const PROJECT_NAME = 'pporlock';
export const HOMEPAGE = 'https://github.com/thowland/pporlock';
export const ISSUES = 'https://github.com/thowland/pporlock/issues';
export const LICENSE = 'GPL-3.0-or-later';
export const LICENSE_URL = 'https://www.gnu.org/licenses/gpl-3.0.html';
export const COPYRIGHT = '© 2025 Tim Howland';

/**
 * What the thing is, in the two sentences someone who just installed it needs.
 *
 * It says "decrypts" out loud. An interception proxy that is coy about what it
 * does to TLS is a security problem dressed as a product, and the about box is
 * exactly where a user goes to find out what they have installed.
 */
export const SUMMARY =
  'pporlock is a local HTTPS interception proxy for a single machine. It decrypts, ' +
  'inspects and can rewrite your browser’s traffic using modules you write yourself, ' +
  'so you can see and change what a site sends and receives.';

/**
 * The part that is not marketing. Module code is trusted and unsandboxed — the
 * project treats that as its trust model rather than a defect (SECURITY.md), so
 * every surface that introduces the tool has to say it.
 */
export const TRUST_NOTE =
  'Modules run as ordinary Python, unsandboxed, with full access to intercepted ' +
  'traffic — including during a dry run. Only enable modules you have read.';

/** Paths into the web UI, appended to the daemon's control origin. */
export const WEB_UI_HELP = '/#/help';
export const WEB_UI_ABOUT = '/#/about';

/**
 * The about page, as an extension-relative path.
 *
 * A constant because it is reached only through `chrome.runtime.getURL()` — a
 * string, invisible to the bundler — which is exactly how `panel.html` came to
 * be registered nowhere and shipped as a blank tab (OI-28). `build-inputs.test`
 * follows this reference so the same thing cannot happen twice.
 */
export const ABOUT_PAGE = 'src/popup/about.html';
