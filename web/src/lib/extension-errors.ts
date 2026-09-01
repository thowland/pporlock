/**
 * The extension's error states, written out for the help view (REQ EXT-024).
 *
 * The popup already explains each of these at the moment it happens. This is
 * the other half: the popup is 330px and gone the instant you click away, so
 * there has to be somewhere you can read the whole set — including the one you
 * saw yesterday and cannot now reproduce.
 *
 * **The prose here is a second copy, and the codes are not.** The catalogue in
 * `extension/src/shared/errors.ts` is the extension's; this one is the help's,
 * in a longer register with the diagnosis a popup has no room for. What must
 * never differ is *which errors exist*: an error state the extension can enter
 * and the help does not mention is precisely the state someone will be looking
 * up. `extension-errors.test.ts` reads the extension's own source and fails if
 * the two sets of codes diverge in either direction.
 */

export interface ExtensionErrorHelp {
  /** The `ExtErrorCode` the extension records, shown as the popup's tooltip. */
  code: string;
  title: string;
  /** What is actually happening, at more length than a popup can afford. */
  cause: string;
  /** What to do, in order. Each entry is one step. */
  fix: string[];
}

export const EXTENSION_ERRORS: ExtensionErrorHelp[] = [
  {
    code: 'daemon_unreachable',
    title: 'The daemon is not responding',
    cause:
      'Nothing is listening on the control port. The extension turned Chrome’s proxy off so ' +
      'your browsing keeps working — nothing is being intercepted, and nothing is being lost.',
    fix: [
      'Start it: `pporlock run` in a terminal, or `make start` from a checkout.',
      'Check it came up: `pporlock status`.',
      'Turn the proxy back on from the popup. It does not re-arm itself, on purpose.',
    ],
  },
  {
    code: 'daemon_unresponsive',
    title: 'The daemon stopped answering in time',
    cause:
      'The socket accepted the connection but the health check timed out. This usually means ' +
      'the daemon is alive and busy rather than dead — a large download through the proxy can ' +
      'do it. The proxy was still turned off, because the extension cannot tell the difference ' +
      'from outside and a wedged proxy breaks browsing entirely.',
    fix: [
      'Run `pporlock status`. If it answers, the daemon is fine.',
      'Turn the proxy back on.',
      'If it happens repeatedly under load, see the throughput note in the troubleshooting guide.',
    ],
  },
  {
    code: 'unpaired',
    title: 'Not paired with the daemon',
    cause:
      'The extension can see the daemon but holds no token, so every authenticated route is ' +
      'refused. The proxy itself still works; counts, profiles and recording do not.',
    fix: [
      'Run `pporlock pair`. It prints a code that is valid for a few minutes.',
      'Type the code into the popup and press pair.',
    ],
  },
  {
    code: 'token_rejected',
    title: 'The daemon rejected the token',
    cause:
      'The token the extension holds is no longer one the daemon recognises — it was rotated, ' +
      'or the state directory was reset or moved. The extension has discarded it and is now ' +
      'unpaired.',
    fix: ['Run `pporlock pair` again and enter the fresh code.'],
  },
  {
    code: 'proxy_not_controllable',
    title: 'Chrome will not let pporlock set the proxy',
    cause:
      'Another extension holds Chrome’s proxy setting, or an enterprise policy does. pporlock ' +
      'will not fight another extension for it: two extensions taking turns would leave the ' +
      'browser in whichever state lost the race, silently.',
    fix: [
      'Find the other proxy or VPN extension and disable it.',
      'If the popup says the setting is controlled by policy, the machine’s administrator owns it.',
      'Alternatively, point Chrome at the proxy yourself and leave the extension’s toggle off.',
    ],
  },
  {
    code: 'proxy_set_failed',
    title: 'Setting the proxy failed',
    cause:
      'Chrome refused the configuration outright. Traffic is still going direct. In scoped mode ' +
      'this is most often a PAC script Chrome would not accept — usually a malformed host ' +
      'pattern in the scoped host list.',
    fix: [
      'Try the toggle again.',
      'If you are in scoped mode, check the host patterns in the options page.',
      'Reload the extension from chrome://extensions.',
    ],
  },
  {
    code: 'attribution_overflow',
    title: 'Too many requests to attribute',
    cause:
      'Per-tab attribution keeps a bounded buffer of in-flight requests, and it filled. Some ' +
      'flows will show no tab until traffic settles. Interception is completely unaffected — ' +
      'this only costs you the tab column.',
    fix: ['Nothing. It clears itself. Flows with no tab are still captured in full.'],
  },
  {
    code: 'sse_disconnected',
    title: 'Lost the live event stream',
    cause:
      'The event stream between the extension and the daemon dropped. Counts and module health ' +
      'may be stale. The proxy is still running and still intercepting.',
    fix: [
      'It reconnects on its own.',
      'Reopen the popup to force a refresh sooner.',
      'If it never reconnects, the daemon is probably gone — check `pporlock status`.',
    ],
  },
];
