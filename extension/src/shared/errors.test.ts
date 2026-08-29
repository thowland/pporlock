import { describe, expect, it } from 'vitest';
import { ERROR_PRESENTATION, describeError } from './errors';
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
