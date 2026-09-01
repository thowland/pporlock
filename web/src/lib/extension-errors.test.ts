/**
 * The help's error list against the extension's, read from its source.
 *
 * This test exists because of the pattern the project has been bitten by four
 * times: two components hold the same idea, nothing checks they agree, and the
 * one that is only read by humans quietly stops being true. Help text is the
 * worst place for that — it is consulted exactly when someone is already stuck,
 * and an error state with no entry sends them looking for a page that is not
 * there.
 *
 * It reads `extension/src/shared/state.ts` rather than importing it, because
 * the web build must not depend on the extension build. That makes the coupling
 * a string match, which is why the parse is asserted non-vacuous first: a regex
 * that silently matched nothing would make this test pass forever.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { EXTENSION_ERRORS } from './extension-errors';

/** The members of the `ExtErrorCode` union the extension actually declares. */
function extensionErrorCodes(): string[] {
  // Test-only, a literal path resolved against this file's own location.
  const path = resolve(import.meta.dirname, '../../../extension/src/shared/state.ts');
  // eslint-disable-next-line security/detect-non-literal-fs-filename
  const source = readFileSync(path, 'utf8');
  const start = source.indexOf('export type ExtErrorCode');
  const union = source.slice(start, source.indexOf(';', start));
  return [...union.matchAll(/'([a-z_]+)'/g)].map((m) => m[1] as string);
}

describe('the help’s account of the extension’s error states', () => {
  it('found the extension’s union at all, so nothing below is vacuous', () => {
    // If the declaration is renamed or reshaped, this fails loudly rather than
    // letting an empty match set agree with everything.
    expect(extensionErrorCodes().length).toBeGreaterThan(4);
  });

  it('documents every error the extension can record', () => {
    const documented = new Set(EXTENSION_ERRORS.map((e) => e.code));
    const missing = extensionErrorCodes().filter((code) => !documented.has(code));
    expect(missing).toEqual([]);
  });

  it('documents no error the extension cannot record', () => {
    // The other direction matters too: help for a state that no longer exists
    // sends someone chasing a cause that cannot be theirs.
    const declared = new Set(extensionErrorCodes());
    const stale = EXTENSION_ERRORS.map((e) => e.code).filter((code) => !declared.has(code));
    expect(stale).toEqual([]);
  });

  it('gives every error at least one thing to do', () => {
    // Including the two that are not the user's to fix: "nothing, it clears
    // itself" is an answer, and a blank space is not.
    for (const error of EXTENSION_ERRORS) {
      expect(error.fix.length).toBeGreaterThan(0);
      expect(error.cause.length).toBeGreaterThan(40);
    }
  });

  it('lists each code once', () => {
    const codes = EXTENSION_ERRORS.map((e) => e.code);
    expect(new Set(codes).size).toBe(codes.length);
  });
});
