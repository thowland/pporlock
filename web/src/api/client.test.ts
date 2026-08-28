import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, ApiRequestError } from './client';
import { NonLoopbackOriginError } from '../lib/control-origin';

const ORIGIN = 'http://127.0.0.1:8081';

function mockFetch(response: Partial<Response> & { jsonBody?: unknown }) {
  return vi.fn().mockResolvedValue({
    ok: response.ok ?? true,
    status: response.status ?? 200,
    statusText: response.statusText ?? 'OK',
    json: async () => response.jsonBody ?? {},
  } as Response);
}

describe('ApiClient', () => {
  let fetchMock: ReturnType<typeof mockFetch>;

  beforeEach(() => {
    fetchMock = mockFetch({ jsonBody: { ok: true } });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('refuses a non-loopback origin at construction', () => {
    // REQ API-010 — enforced at the boundary, not discovered in production.
    expect(() => new ApiClient('https://evil.example')).toThrow(NonLoopbackOriginError);
  });

  it('sends no Authorization header before a token is set', async () => {
    await new ApiClient(ORIGIN).health();
    expect(fetchMock.mock.calls[0]?.[1]?.headers).not.toHaveProperty('Authorization');
  });

  it('sends the bearer token once set', async () => {
    const api = new ApiClient(ORIGIN);
    api.setToken('secret');
    await api.getState();
    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers['Authorization']).toBe('Bearer secret');
  });

  it('sends the client header on every request, reads included  # REQ API-013, CAP-043', async () => {
    // Replaces "sends the client header on mutating requests only". The header
    // is no longer mutation-scoped: `GET /flows/{id}?unmask=` requires it too,
    // because unmasking is web-UI-only by construction (SPEC-0 §9.3), and the
    // daemon refuses the read outright without it.
    const api = new ApiClient(ORIGIN);
    api.setToken('secret');

    await api.getState();
    const readHeaders = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(readHeaders['X-Pporlock-Client']).toBe('ui');
    // Reads still carry no JSON content type — there is no body to describe.
    expect(readHeaders).not.toHaveProperty('Content-Type');

    await api.setDevToggles({ anticomp: true });
    const writeHeaders = fetchMock.mock.calls[1]?.[1]?.headers as Record<string, string>;
    // REQ API-013 — this is what a cross-origin form cannot produce.
    expect(writeHeaders['X-Pporlock-Client']).toBe('ui');
    expect(writeHeaders['Content-Type']).toBe('application/json');
  });

  it('never puts the token in a URL', async () => {
    // A URL lands in logs, history, and Referer, and this token grants read
    // access to captured traffic (implementation-plan.md §2.5).
    const api = new ApiClient(ORIGIN);
    api.setToken('super-secret-token');
    await api.listFlows({ host: 'a.example' });
    expect(String(fetchMock.mock.calls[0]?.[0])).not.toContain('super-secret-token');
    expect(api.eventsUrl({ host: 'a.example' })).not.toContain('super-secret-token');
  });

  it('puts the token in the stream headers instead', () => {
    const api = new ApiClient(ORIGIN);
    api.setToken('secret');
    expect(api.streamHeaders()['Authorization']).toBe('Bearer secret');
  });

  it('passes Last-Event-ID on resume', () => {
    expect(new ApiClient(ORIGIN).streamHeaders('42')['Last-Event-ID']).toBe('42');
  });

  it('serializes filters into query parameters', async () => {
    const api = new ApiClient(ORIGIN);
    await api.listFlows({ host: 'a.example', modified: true }, { limit: 50, detail: 'summary' });
    const url = String(fetchMock.mock.calls[0]?.[0]);
    expect(url).toContain('host=a.example');
    expect(url).toContain('modified=true');
    expect(url).toContain('limit=50');
    expect(url).toContain('detail=summary');
  });

  it('omits unset filter fields', async () => {
    const api = new ApiClient(ORIGIN);
    await api.listFlows({ q: '' });
    expect(String(fetchMock.mock.calls[0]?.[0])).not.toContain('host=');
  });

  it('surfaces the daemon error code', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ok: false,
        status: 401,
        jsonBody: { error: { code: 'unauthorized', message: 'no token' } },
      }),
    );
    const api = new ApiClient(ORIGIN);
    await expect(api.getState()).rejects.toThrow(ApiRequestError);
    await expect(api.getState()).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
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
    await expect(new ApiClient(ORIGIN).getState()).rejects.toMatchObject({ status: 502 });
  });

  it('handles a 204 with no body', async () => {
    vi.stubGlobal('fetch', mockFetch({ status: 204 }));
    await expect(new ApiClient(ORIGIN).clearFlows()).resolves.toBeUndefined();
  });

  it('reports whether a token is present', () => {
    const api = new ApiClient(ORIGIN);
    expect(api.hasToken).toBe(false);
    api.setToken('x');
    expect(api.hasToken).toBe(true);
    api.setToken(null);
    expect(api.hasToken).toBe(false);
  });
});
