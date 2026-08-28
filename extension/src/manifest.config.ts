/**
 * MV3 manifest (SPEC-3 §2.3).
 *
 * Permissions are deliberately minimal, and each one is justified because each
 * is a real cost:
 *
 *   proxy       the extension's core function — nothing else can configure
 *               Chrome's proxy from inside the browser
 *   storage     durable state (paired token, proxy on/off) and session state
 *               (per-tab counters). An MV3 service worker is terminated
 *               aggressively, so nothing may live only in its memory.
 *   tabs        mapping tab ids to URLs for the popup and, later, the panel
 *   alarms      the health-check heartbeat. A suspended worker cannot notice
 *               that the daemon died, and noticing is the whole point of §4.4.
 *   webRequest  observation only, for tab attribution in Sprint 6. There is no
 *               webRequestBlocking: interception is the daemon's job.
 *
 * host_permissions covers loopback on any port, not just the default 8081. The
 * control port is configurable (config.control.listen_port), and an extension
 * pinned to one port is simply broken for anyone who changes it — which an E2E
 * test running the daemon on an ephemeral port found immediately. This is still
 * loopback only: the extension cannot read any page, or reach any host that is
 * not this machine.
 *
 * optional_host_permissions is <all_urls>, and it is NOT granted at install.
 *
 * The OI-2 spike established that chrome.webRequest only reports requests the
 * extension has host access to: with loopback-only permissions, attribution
 * coverage measured 0%; with <all_urls>, 100%. So per-tab attribution genuinely
 * requires broad host access — REQ EXT-001 assumed otherwise.
 *
 * Rather than take it at install, it is optional and requested when the user
 * asks for per-tab attribution. Installing pporlock therefore prompts for
 * nothing broad, the cost is paid only by someone who wants the feature, and
 * everything else — proxy control, the fail-safe, browser-wide counts — works
 * without it. Without the grant the daemon simply reports flows with no tab,
 * which every consumer already tolerates (SPEC-0 §3.6).
 */
// Declared with a local type rather than CRXJS's ManifestV3Export: that is a
// union including a Promise, which makes the object's own fields unreadable to
// a test — and this file is a security surface worth asserting on directly.
interface Manifest {
  manifest_version: 3;
  name: string;
  version: string;
  description: string;
  minimum_chrome_version?: string;
  permissions: string[];
  host_permissions: string[];
  optional_host_permissions?: string[];
  background: { service_worker: string; type: 'module' };
  action: { default_popup: string; default_title?: string };
  options_page?: string;
  devtools_page?: string;
  content_scripts?: { matches: string[]; js: string[]; run_at?: string }[];
}

const manifest: Manifest = {
  manifest_version: 3,
  name: 'pporlock',
  version: '0.1.0',
  description: 'Control and observe the pporlock local interception proxy.',
  minimum_chrome_version: '116',
  permissions: ['proxy', 'storage', 'tabs', 'alarms', 'webRequest', 'notifications'],
  host_permissions: ['http://127.0.0.1/*', 'http://localhost/*'],
  optional_host_permissions: ['<all_urls>'],
  background: {
    service_worker: 'src/background/index.ts',
    type: 'module',
  },
  action: {
    default_popup: 'src/popup/index.html',
    default_title: 'pporlock',
  },
  options_page: 'src/popup/options.html',
  // <all_urls> here is the same grant attribution needs, and the banner is
  // inert without it: the content script only ever renders what the service
  // worker sends it (SPEC-3 §8).
  content_scripts: [
    {
      matches: ['http://*/*', 'https://*/*'],
      js: ['src/content/banner.ts'],
      run_at: 'document_idle',
    },
  ],
  devtools_page: 'src/devtools/devtools.html',
};

export default manifest;
