/** Session, dry-run, config and unmask routes. SPEC-0 §6.8, §6.9, §9.3. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';
import { makeDryRun, makeSession } from '../test/factories';

const ORIGIN = 'http://127.0.0.1:8081';

let fetchMock: ReturnType<typeof vi.fn>;

function ok(payload: unknown, status = 200) {
  return {
    ok: true,
    status,
    statusText: 'OK',
    json: () => Promise.resolve(payload),
  };
}

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(ok({}));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function urlOf(call = 0): string {
  return String(fetchMock.mock.calls[call]?.[0]);
}

function bodyOf(call = 0): unknown {
  const init = fetchMock.mock.calls[call]?.[1] as RequestInit | undefined;
  return init?.body === undefined ? undefined : JSON.parse(String(init.body));
}

describe('sessions  # REQ CAP-020, CAP-021, CAP-024', () => {
  it('lists, reads, starts, stops, renames and deletes', async () => {
    const api = new ApiClient(ORIGIN);
    fetchMock.mockResolvedValue(ok([makeSession()]));
    expect(await api.listSessions()).toHaveLength(1);
    expect(urlOf()).toBe(`${ORIGIN}/sessions`);

    fetchMock.mockResolvedValue(ok(makeSession()));
    await api.getSession('s 1');
    expect(urlOf(1)).toBe(`${ORIGIN}/sessions/s%201`);

    await api.startRecording('checkout');
    expect(urlOf(2)).toBe(`${ORIGIN}/sessions`);
    expect(bodyOf(2)).toEqual({ name: 'checkout' });

    await api.stopRecording('s1');
    expect(urlOf(3)).toBe(`${ORIGIN}/sessions/s1/stop`);

    await api.renameSession('s1', 'renamed');
    expect(bodyOf(4)).toEqual({ name: 'renamed' });

    fetchMock.mockResolvedValue({ ok: true, status: 204, statusText: 'No Content' });
    await api.deleteSession('s1');
    expect(fetchMock.mock.calls[5]?.[1]).toMatchObject({ method: 'DELETE' });
  });

  it('queries session flows with the live filter vocabulary', async () => {
    // Same shape as listFlows, deliberately: the session browser reuses the
    // live table and detail components (SPEC-2 §8.2).
    const api = new ApiClient(ORIGIN);
    fetchMock.mockResolvedValue(ok({ flows: [], next_cursor: null, total_estimate: 0 }));
    await api.listSessionFlows('s1', { host: 'a.example', blocked: true }, { limit: 50 });
    const url = new URL(urlOf());
    expect(url.pathname).toBe('/sessions/s1/flows');
    expect(url.searchParams.get('host')).toBe('a.example');
    expect(url.searchParams.get('blocked')).toBe('true');
    expect(url.searchParams.get('limit')).toBe('50');
  });

  it('builds export URLs for both formats, with no token in them', () => {
    const api = new ApiClient(ORIGIN);
    api.setToken('super-secret-token');
    expect(api.sessionExportUrl('s1', 'har')).toBe(`${ORIGIN}/sessions/s1/export?format=har`);
    expect(api.sessionExportUrl('s1', 'pporlock')).not.toContain('super-secret-token');
  });
});

describe('dry run  # REQ CAP-030, CAP-033', () => {
  it('posts the candidate to the session it runs against', async () => {
    const api = new ApiClient(ORIGIN);
    fetchMock.mockResolvedValue(ok(makeDryRun()));
    const result = await api.dryRun('s1', { use_installed: ['candidate'], limit: 200 });
    expect(urlOf()).toBe(`${ORIGIN}/sessions/s1/dryrun`);
    expect(bodyOf()).toEqual({ use_installed: ['candidate'], limit: 200 });
    expect(result.summary.matched).toBe(63);
  });
});

describe('config  # REQ CAP-044', () => {
  it('reads the effective configuration and writes back only edited sections', async () => {
    const api = new ApiClient(ORIGIN);
    fetchMock.mockResolvedValue(
      ok({ redaction: { enabled: true, header_patterns: [], json_key_patterns: [] } }),
    );
    await api.getConfig();
    expect(urlOf()).toBe(`${ORIGIN}/config`);

    await api.putConfig({ redaction: { enabled: false } });
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({ method: 'PUT' });
    expect(bodyOf(1)).toEqual({ redaction: { enabled: false } });
  });
});

describe('unmask  # REQ CAP-043, SPEC-0 §9.3', () => {
  it('reveals one value at a time from the live flow route', async () => {
    const api = new ApiClient(ORIGIN);
    fetchMock.mockResolvedValue(
      ok({ flow_id: 'f0', field_path: 'request.headers.cookie', value: 'sid=abc' }),
    );
    const result = await api.unmask('f0', 'request.headers.cookie');
    const url = new URL(urlOf());
    expect(url.pathname).toBe('/flows/f0');
    expect(url.searchParams.get('unmask')).toBe('request.headers.cookie');
    expect(result.value).toBe('sid=abc');
  });

  it('identifies itself as the UI, which is what the daemon requires', async () => {
    // The daemon refuses the unmask read without this header, because
    // unmasking is web-UI-only by construction (REQ MCP-003).
    const api = new ApiClient(ORIGIN);
    fetchMock.mockResolvedValue(ok({ flow_id: 'f0', field_path: 'x', value: 'y' }));
    await api.unmask('f0', 'request.headers.cookie');
    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers['X-Pporlock-Client']).toBe('ui');
  });

  it('has no session equivalent', () => {
    // Structural, not incidental: a session flow was redacted before it
    // reached the file (REQ CAP-045), so there is nothing to reveal. If a
    // method ever appears here, the contract has been broken.
    const surface = Object.getOwnPropertyNames(ApiClient.prototype);
    expect(surface.filter((name) => name.toLowerCase().includes('unmask'))).toEqual(['unmask']);
  });
});
