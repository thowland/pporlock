/** Mask format detection and rendering. SPEC-0 §9.1, REQ CAP-042. */
import { describe, expect, it } from 'vitest';
import { describeMasked, headerFieldPath, isMasked, parseMasked } from './redaction';

describe('parseMasked  # REQ CAP-042', () => {
  it('parses the fixed mask format', () => {
    expect(parseMasked('«redacted:sha1=a3f2,len=142»')).toEqual({ sha1: 'a3f2', len: 142 });
  });

  it('leaves an ordinary value alone', () => {
    expect(parseMasked('text/html')).toBeNull();
    expect(isMasked('text/html')).toBe(false);
  });

  it('refuses a value that merely contains the marker', () => {
    // An unanchored pattern would let a captured header value fake a redaction,
    // which is how a real secret gets rendered as though it were hidden.
    expect(parseMasked('prefix «redacted:sha1=a3f2,len=142»')).toBeNull();
    expect(parseMasked('«redacted:sha1=a3f2,len=142» suffix')).toBeNull();
  });

  it('refuses a malformed hash or length', () => {
    expect(parseMasked('«redacted:sha1=A3F2,len=142»')).toBeNull(); // uppercase
    expect(parseMasked('«redacted:sha1=a3f,len=142»')).toBeNull(); // three hex
    expect(parseMasked('«redacted:sha1=a3f2,len=»')).toBeNull();
  });

  it('describes a masked value the way the extension does', () => {
    // The two clients must agree, or one will show what the other hides.
    expect(describeMasked('«redacted:sha1=a3f2,len=142»')).toBe('redacted · 142 bytes · #a3f2');
    expect(describeMasked('not masked')).toBe('not masked');
  });

  it('preserves enough to compare two values without revealing either', () => {
    const one = parseMasked('«redacted:sha1=a3f2,len=142»');
    const two = parseMasked('«redacted:sha1=a3f2,len=142»');
    const other = parseMasked('«redacted:sha1=bb01,len=142»');
    expect(one).toEqual(two);
    expect(one).not.toEqual(other);
  });
});

describe('headerFieldPath  # REQ CAP-043', () => {
  it('omits the occurrence for the first header of a name', () => {
    expect(headerFieldPath('request', 'Cookie', 0)).toBe('request.headers.cookie');
  });

  it('indexes repeats zero-based, as the daemon resolves them', () => {
    // daemon/src/pporlock/capture/redact.py:_from_headers — the third
    // Set-Cookie is `.2`, not `.3`.
    expect(headerFieldPath('response', 'Set-Cookie', 2)).toBe('response.headers.set-cookie.2');
  });
});
