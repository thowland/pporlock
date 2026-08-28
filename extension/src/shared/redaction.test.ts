import { describe, expect, it } from 'vitest';
import { describeMasked, isMasked, parseMasked } from './redaction';

const MASKED = '«redacted:sha1=a1b2,len=48»';

describe('masked value parsing', () => {
  it('parses the contract format', () => {
    expect(parseMasked(MASKED)).toEqual({ sha1: 'a1b2', len: 48 });
  });

  it('treats an ordinary value as unmasked', () => {
    expect(isMasked('Bearer abc123')).toBe(false);
    expect(parseMasked('Bearer abc123')).toBeNull();
  });

  it('does not match a value that merely embeds the marker', () => {
    // An unanchored pattern would match here, and the panel would then hide a
    // real value it should have shown — or worse, imply a secret was protected
    // when it was not.
    expect(isMasked(`prefix ${MASKED} suffix`)).toBe(false);
  });

  it('rejects a malformed fingerprint', () => {
    expect(isMasked('«redacted:sha1=zzzz,len=48»')).toBe(false);
    expect(isMasked('«redacted:sha1=a1b2c3,len=48»')).toBe(false);
    expect(isMasked('«redacted:sha1=a1b2,len=»')).toBe(false);
  });

  it('describes length and fingerprint without inventing a value', () => {
    const text = describeMasked(MASKED);
    expect(text).toContain('48 bytes');
    expect(text).toContain('#a1b2');
    expect(text).not.toContain('redacted:sha1');
  });

  it('lets two masked values be compared without revealing either', () => {
    // The whole point of carrying a fingerprint (REQ CAP-042).
    const a = parseMasked('«redacted:sha1=a1b2,len=48»');
    const b = parseMasked('«redacted:sha1=a1b2,len=48»');
    const c = parseMasked('«redacted:sha1=99ff,len=48»');
    expect(a).toEqual(b);
    expect(a).not.toEqual(c);
  });

  it('passes an unmasked value through describe unchanged', () => {
    expect(describeMasked('text/html')).toBe('text/html');
  });
});
