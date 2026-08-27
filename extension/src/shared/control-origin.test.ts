import { describe, it, expect } from 'vitest';
import {
  assertLoopbackOrigin,
  isLoopbackHost,
  resolveControlOrigin,
  NonLoopbackOriginError,
  DEFAULT_CONTROL_ORIGIN,
} from './control-origin';

describe('isLoopbackHost', () => {
  it.each(['127.0.0.1', 'localhost', 'LOCALHOST', '[::1]', '::1'])('accepts %s', (h) => {
    expect(isLoopbackHost(h)).toBe(true);
  });

  it.each(['0.0.0.0', '192.168.1.5', 'example.com', '127.0.0.1.evil.com'])('rejects %s', (h) => {
    expect(isLoopbackHost(h)).toBe(false);
  });
});

describe('assertLoopbackOrigin — REQ API-010', () => {
  it('returns the normalised origin for loopback', () => {
    expect(assertLoopbackOrigin('http://127.0.0.1:8081/')).toBe('http://127.0.0.1:8081');
  });

  it('rejects a non-loopback host', () => {
    expect(() => assertLoopbackOrigin('http://example.com:8081')).toThrow(NonLoopbackOriginError);
  });

  it('rejects 0.0.0.0, which binds every interface', () => {
    expect(() => assertLoopbackOrigin('http://0.0.0.0:8081')).toThrow(NonLoopbackOriginError);
  });

  it('rejects a non-http scheme', () => {
    expect(() => assertLoopbackOrigin('file:///etc/passwd')).toThrow(NonLoopbackOriginError);
  });

  it('rejects unparseable input', () => {
    expect(() => assertLoopbackOrigin('not a url')).toThrow(NonLoopbackOriginError);
  });

  it('carries the offending origin on the error', () => {
    try {
      assertLoopbackOrigin('http://evil.example');
      expect.unreachable('should have thrown');
    } catch (e) {
      expect((e as NonLoopbackOriginError).origin).toBe('http://evil.example');
    }
  });
});

describe('resolveControlOrigin', () => {
  it('defaults to the loopback control port', () => {
    expect(resolveControlOrigin()).toBe(DEFAULT_CONTROL_ORIGIN);
  });

  it('accepts an explicit loopback origin', () => {
    expect(resolveControlOrigin('http://localhost:9999')).toBe('http://localhost:9999');
  });

  it('refuses an explicit non-loopback origin', () => {
    expect(() => resolveControlOrigin('https://pporlock.example')).toThrow(NonLoopbackOriginError);
  });
});
