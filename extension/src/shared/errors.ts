/**
 * Error codes rendered for humans (SPEC-3 §10, REQ EXT-024).
 *
 * Every `ExtErrorCode` gets three things, and the split matters:
 *
 *   title    what happened, in the user's terms — never the enum value
 *   meaning  what it implies for their browsing right now, because the first
 *            question after "something broke" is always "is my browser ok"
 *   remedy   the single next action, phrased as an instruction
 *
 * A code with no entry would surface as a bare identifier, which is the exact
 * failure this table exists to prevent, so `errors.test.ts` iterates the union
 * and fails if one is ever added without a description.
 */
import { ApiError } from './api';
import type { ExtErrorCode } from './state';

export interface ErrorPresentation {
  title: string;
  meaning: string;
  remedy: string;
  /** Whether this is a state the user is expected to fix, not merely observe. */
  actionable: boolean;
}

/**
 * Declared as a Record keyed by the union, so adding a code to `ExtErrorCode`
 * without adding an entry here is a type error rather than a runtime surprise.
 */
export const ERROR_PRESENTATION: Record<ExtErrorCode, ErrorPresentation> = {
  daemon_unreachable: {
    title: 'The pporlock daemon is not responding',
    meaning:
      'The proxy was turned off so your browsing keeps working. Nothing is being intercepted.',
    remedy: 'Start it with `pporlock run`, then turn the proxy back on.',
    actionable: true,
  },
  daemon_unresponsive: {
    title: 'The pporlock daemon stopped answering in time',
    meaning:
      'The daemon may still be running and simply overloaded — a busy proxy can miss ' +
      'its health checks. The proxy was turned off so your browsing keeps working.',
    remedy: 'Check it with `pporlock status`. If it is running, turn the proxy back on.',
    actionable: true,
  },
  unpaired: {
    title: 'Not paired with the daemon',
    meaning:
      'The extension can see the daemon but has no token, so it cannot read counts or change profiles.',
    remedy: 'Run `pporlock pair` and enter the code it prints.',
    actionable: true,
  },
  token_rejected: {
    title: 'The daemon rejected this extension’s token',
    meaning:
      'The token was rotated or the daemon’s state directory was reset. The extension is now unpaired.',
    remedy: 'Run `pporlock pair` again to get a fresh code.',
    actionable: true,
  },
  proxy_not_controllable: {
    title: 'Chrome will not let pporlock set the proxy',
    meaning:
      'Another extension or an enterprise policy holds the proxy setting. pporlock will not fight it.',
    remedy:
      'Disable the other proxy extension, or ask whoever manages this machine about the policy.',
    actionable: true,
  },
  proxy_set_failed: {
    title: 'Setting the proxy failed',
    meaning: 'Chrome refused the configuration, so traffic is still going direct.',
    remedy: 'Try the toggle again. If it keeps failing, reload the extension.',
    actionable: true,
  },
  attribution_overflow: {
    title: 'Too many requests to attribute',
    meaning:
      'The per-tab attribution buffer filled up, so some flows will show no tab. Interception is unaffected.',
    remedy: 'Nothing to do — it clears on its own once traffic settles.',
    actionable: false,
  },
  sse_disconnected: {
    title: 'Lost the live event stream',
    meaning: 'Counts and module health may be stale. The proxy itself is still running normally.',
    remedy: 'It reconnects automatically; reopen the popup to force a refresh.',
    actionable: false,
  },
};

/** Never returns undefined: an unknown code still gets something readable. */
export function describeError(code: string): ErrorPresentation {
  if (Object.hasOwn(ERROR_PRESENTATION, code)) {
    return ERROR_PRESENTATION[code as ExtErrorCode];
  }
  return {
    title: 'Something went wrong',
    meaning: `The daemon reported an error this version does not recognise (${code}).`,
    remedy: 'Check the daemon log with `pporlock doctor`.',
    actionable: false,
  };
}

/**
 * Which `ExtErrorCode` an HTTP failure means, or null when the failure was not
 * an HTTP response at all.
 *
 * The distinction this draws is the one the extension was missing entirely: a
 * daemon that answers 403 is *alive and refusing us*, which is a completely
 * different fact from a daemon that is not there. Both used to arrive at the
 * popup as "the daemon is not responding", and the remedy printed for that —
 * `pporlock run` — sends the user to restart the one thing that is working.
 *
 * 401 means the bearer token was missing or rejected. 403 in practice means the
 * origin allowlist: the daemon checks the origin before anything else, so an
 * unpaired extension gets 403 on every route including the unauthenticated
 * ones. A 403 can also mean a bad `x-pporlock-client` header, but our own
 * client always sends a valid one, so treating 403 as unpaired is right for
 * every case this extension can actually produce.
 */
export function classifyApiError(error: unknown): ExtErrorCode | null {
  if (!(error instanceof ApiError)) return null;
  if (error.status === 401) return 'token_rejected';
  if (error.status === 403) return 'unpaired';
  return null;
}

/** The one-line remedy for an error, for the surfaces that show a bare string. */
export function remedyFor(code: ExtErrorCode): string {
  const presentation = describeError(code);
  return `${presentation.title}. ${presentation.remedy}`;
}

/** What to say when the daemon could not be reached at all. */
export const DAEMON_UNREACHABLE_MESSAGE = 'Cannot reach the daemon. Start it with `pporlock run`.';

/**
 * The message for a failed daemon call, as a single decision that can be
 * tested without a service worker.
 *
 * It lives here rather than inline in the worker because inline is where it was
 * wrong: `enableProxy` caught every failure and reported the unreachable text,
 * so the one moment the user was most likely to be looking — turning the proxy
 * on — was the moment the extension was most confidently misdiagnosing. There
 * is no test file for the worker, so the decision could not be pinned until it
 * was a function.
 */
export function daemonFailureMessage(error: unknown): string {
  const code = classifyApiError(error);
  return code === null ? DAEMON_UNREACHABLE_MESSAGE : remedyFor(code);
}
