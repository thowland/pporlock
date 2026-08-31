import { describe, expect, it } from 'vitest';
import { ApiError } from './api';
import {
  DAEMON_UNREACHABLE_MESSAGE,
  ERROR_PRESENTATION,
  classifyApiError,
  daemonFailureMessage,
  describeError,
} from './errors';
import type { ExtErrorCode } from './state';

/**
 * The list is duplicated here on purpose. `ExtErrorCode` is a type, so it is
 * gone at runtime; writing it out means a code added to the union but not to
 * this list fails the type check below, and a code added to both but missing
 * from the table fails the assertion.
 */
const ALL_CODES: ExtErrorCode[] = [
  'daemon_unreachable',
  'daemon_unresponsive',
  'unpaired',
  'token_rejected',
  'proxy_not_controllable',
  'proxy_set_failed',
  'attribution_overflow',
  'sse_disconnected',
];

describe('error presentation', () => {
  it('describes every error code', () => {
    expect(Object.keys(ERROR_PRESENTATION).sort()).toEqual([...ALL_CODES].sort());
  });

  it.each(ALL_CODES)('%s reads as prose, not as an enum', (code) => {
    const p = ERROR_PRESENTATION[code];
    expect(p.title.length).toBeGreaterThan(10);
    expect(p.meaning.length).toBeGreaterThan(20);
    expect(p.remedy.length).toBeGreaterThan(10);
    // The identifier itself must never be the thing the user is shown.
    expect(p.title).not.toContain(code);
  });

  it('says what it means for browsing when the daemon dies', () => {
    // The first question after any failure is whether the browser still works.
    expect(ERROR_PRESENTATION.daemon_unreachable.meaning).toMatch(/browsing keeps working/);
  });

  it('falls back readably for a code it has never heard of', () => {
    const p = describeError('from_a_future_version');
    expect(p.title).toBe('Something went wrong');
    expect(p.meaning).toContain('from_a_future_version');
    expect(p.actionable).toBe(false);
  });

  it('returns the real entry for a known code', () => {
    expect(describeError('unpaired')).toBe(ERROR_PRESENTATION.unpaired);
  });
});

describe('classifyApiError — telling a refusal from a corpse', () => {
  // The daemon checks the origin allowlist before the token and before the
  // public-route exemption, so an unpaired extension gets 403 on every route,
  // including unauthenticated /state/health. Reading that as "the daemon is
  // down" is what sent users to restart a daemon that was answering fine.
  it('reads a 403 as an unpaired extension', () => {
    expect(classifyApiError(new ApiError(403, 'unauthorized', 'origin not permitted'))).toBe(
      'unpaired',
    );
  });

  it('reads a 401 as a rejected token', () => {
    expect(
      classifyApiError(new ApiError(401, 'unauthorized', 'missing or invalid bearer token')),
    ).toBe('token_rejected');
  });

  it('claims nothing about a 5xx — that is a daemon in trouble, not a refusal', () => {
    expect(classifyApiError(new ApiError(500, 'internal', 'boom'))).toBeNull();
  });

  it('claims nothing about a transport failure', () => {
    expect(classifyApiError(new TypeError('Failed to fetch'))).toBeNull();
  });
});

describe('daemonFailureMessage', () => {
  it('never shows the wire message for a refusal', () => {
    // "origin not permitted" is true, precise, and useless: it names no action.
    const message = daemonFailureMessage(new ApiError(403, 'unauthorized', 'origin not permitted'));
    expect(message).not.toContain('origin not permitted');
    expect(message).toContain('pporlock pair');
  });

  it('does not tell the user to start a daemon that is already answering', () => {
    // The specific misdiagnosis this replaces.
    expect(
      daemonFailureMessage(new ApiError(403, 'unauthorized', 'origin not permitted')),
    ).not.toBe(DAEMON_UNREACHABLE_MESSAGE);
  });

  it('still says to start the daemon when it really is not there', () => {
    expect(daemonFailureMessage(new TypeError('Failed to fetch'))).toBe(DAEMON_UNREACHABLE_MESSAGE);
  });
});
