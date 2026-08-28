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
  background: { service_worker: string; type: 'module' };
  action: { default_popup: string; default_title?: string };
  options_page?: string;
  content_scripts?: unknown[];
}

const manifest: Manifest = {
  manifest_version: 3,
  name: 'pporlock',
  version: '0.1.0',
  description: 'Control and observe the pporlock local interception proxy.',
  minimum_chrome_version: '116',
  permissions: ['proxy', 'storage', 'tabs', 'alarms', 'webRequest'],
  host_permissions: ['http://127.0.0.1/*', 'http://localhost/*'],
  background: {
    service_worker: 'src/background/index.ts',
    type: 'module',
  },
  action: {
    default_popup: 'src/popup/index.html',
    default_title: 'pporlock',
  },
  options_page: 'src/popup/options.html',
};

export default manifest;
