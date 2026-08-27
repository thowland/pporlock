/**
 * Resolution and validation of the control server origin.
 *
 * The control server binds loopback only and this is asserted, not merely
 * defaulted (REQ API-010, implementation-plan.md §2.5 "Loopback binding").
 * A build or config that points the UI at a non-loopback host is a bug we
 * refuse at the boundary rather than discover in production.
 */

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]', '::1']);

export class NonLoopbackOriginError extends Error {
  constructor(readonly origin: string) {
    super(`Refusing non-loopback control origin: ${origin}`);
    this.name = 'NonLoopbackOriginError';
  }
}

export function isLoopbackHost(host: string): boolean {
  return LOOPBACK_HOSTS.has(host.toLowerCase());
}

/**
 * Validates a control-server origin, returning it normalised without a
 * trailing slash. Throws NonLoopbackOriginError for anything not on loopback.
 */
export function assertLoopbackOrigin(origin: string): string {
  let url: URL;
  try {
    url = new URL(origin);
  } catch {
    throw new NonLoopbackOriginError(origin);
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new NonLoopbackOriginError(origin);
  }
  if (!isLoopbackHost(url.hostname)) {
    throw new NonLoopbackOriginError(origin);
  }
  return url.origin;
}

export const DEFAULT_CONTROL_ORIGIN = 'http://127.0.0.1:8081';

/** The origin the UI talks to. Served same-origin by the daemon (REQ API-003). */
export function resolveControlOrigin(windowOrigin?: string): string {
  return assertLoopbackOrigin(windowOrigin ?? DEFAULT_CONTROL_ORIGIN);
}
