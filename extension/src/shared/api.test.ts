/** Extension control-API client. SPEC-3 §3.2. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, ControlApi } from './api';
import { NonLoopbackOriginError } from './control-origin';

const ORIGIN = 'http://127.0.0.1:8081';

function jsonResponse(body: unknown, ok = true, status = 200) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    statusText: 'x',
    json: async () => body,
  } as Response);
}

afterEach(() => vi.unstubAllGlobals());

describe('ControlApi', () => {
  it('refuses a non-loopback origin', () => {
    expect(() => new ControlApi('https://evil.example')).toThrow(NonLoopbackOriginError);
  });

  it('sends the bearer token', async () => {
    const fetchMock = jsonResponse({});
    vi.stubGlobal('fetch', fetchMock);
    const api = new ControlApi(ORIGIN);
    api.setToken('secret');
    await api.getState();
    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers['Authorization']).toBe('Bearer secret');
  });

  it('identifies itself as the extension on mutating calls', async () => {
    const fetchMock = jsonResponse({});
    vi.stubGlobal('fetch', fetchMock);
    const api = new ControlApi(ORIGIN);
    api.setToken('secret');
    await api.setDevToggles({ anticomp: false });
    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>;
    // REQ API-013, and it is what makes the audit log's origin trustworthy.
    expect(headers['X-Pporlock-Client']).toBe('extension');
  });

  it('never puts the token in a URL', async () => {
    const fetchMock = jsonResponse({});
    vi.stubGlobal('fetch', fetchMock);
    const api = new ControlApi(ORIGIN);
    api.setToken('super-secret');
    await api.getState();
    expect(String(fetchMock.mock.calls[0]?.[0])).not.toContain('super-secret');
  });

  it('pairs without a token, because pairing is how the token arrives', async () => {
    const fetchMock = jsonResponse({ token: 'issued' });
    vi.stubGlobal('fetch', fetchMock);
    const api = new ControlApi(ORIGIN);
    const result = await api.pair('0000-1111');
    expect(result.token).toBe('issued');
    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers).not.toHaveProperty('Authorization');
  });

  it('surfaces the daemon error code', async () => {
    vi.stubGlobal(
      'fetch',
      jsonResponse({ error: { code: 'unauthorized', message: 'nope' } }, false, 401),
    );
    await expect(new ControlApi(ORIGIN).getState()).rejects.toMatchObject({
      status: 401,
      code: 'unauthorized',
    });
  });

  it('handles a non-JSON error body', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 502,
        statusText: 'Bad Gateway',
        json: async () => {
          throw new Error('not json');
        },
      } as unknown as Response),
    );
    await expect(new ControlApi(ORIGIN).getState()).rejects.toThrow(ApiError);
  });

  it('health passes an abort signal so a hung daemon cannot stall the check', async () => {
    const fetchMock = jsonResponse({ ok: true, version: '0.1.0' });
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    await new ControlApi(ORIGIN).health(controller.signal);
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).signal).toBe(controller.signal);
  });

  it('has no unmask capability at all', () => {
    // REQ CAP-043 — unmasking is web-UI only. An extension that cannot unmask
    // cannot be made to leak by a bug.
    const api = new ControlApi(ORIGIN) as unknown as Record<string, unknown>;
    expect(api['unmask']).toBeUndefined();
  });

  it('activates a profile by name', async () => {
    const fetchMock = jsonResponse({ active_profile: 'ad-blocking' });
    vi.stubGlobal('fetch', fetchMock);
    const api = new ControlApi(ORIGIN);
    api.setToken('t');
    await api.activateProfile('ad-blocking');
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('/profiles/ad-blocking/activate');
  });

  it('url-encodes a profile name', async () => {
    const fetchMock = jsonResponse({});
    vi.stubGlobal('fetch', fetchMock);
    const api = new ControlApi(ORIGIN);
    api.setToken('t');
    await api.activateProfile('a/b');
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('a%2Fb');
  });
});
