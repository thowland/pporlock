/**
 * The client against the shapes the daemon actually sends.
 *
 * Every other test in this suite stubs `ApiClient` methods, so every one of
 * them agrees with whatever the client already believed. That is fine for
 * testing components and useless for testing the client — and it is how
 * `listModules` came to expect `{modules: [...]}` while the daemon and
 * `contracts/openapi.yaml` both said a bare array. Nothing caught it until a
 * real daemon rendered "v.modules is not iterable" into a screenshot.
 *
 * So these stub `fetch` instead, with response bodies copied from the contract,
 * and assert the client hands back something usable. A shape change on either
 * side breaks a test here rather than a page in front of a user.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';

const ORIGIN = 'http://127.0.0.1:8081';

function respondWith(body: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function client(): ApiClient {
  const api = new ApiClient(ORIGIN);
  api.setToken('t0ken');
  return api;
}

afterEach(() => vi.unstubAllGlobals());

/** As documented at `/modules` in contracts/openapi.yaml: an array. */
const MODULE_STATUS = {
  name: 'relax-csp',
  version: '0.1.0',
  enabled: true,
  priority: 100,
  state: 'loaded',
  has_python: false,
  rule_count: 3,
  error: null,
  quarantine: null,
  stats: { flows_matched: 0, flows_modified: 0, errors: 0, avg_ms: 0 },
};

describe('list endpoints return bare arrays, not envelopes', () => {
  it('GET /modules', async () => {
    respondWith([MODULE_STATUS]);
    const modules = await client().listModules();
    expect(Array.isArray(modules)).toBe(true);
    expect(modules[0]?.name).toBe('relax-csp');
  });

  it('GET /profiles', async () => {
    // And no `active` field: which profile is active is daemon state, read
    // from GET /state, not a property of the list.
    respondWith([{ name: 'default', modules: [] }]);
    const profiles = await client().listProfiles();
    expect(Array.isArray(profiles)).toBe(true);
    expect(profiles[0]?.name).toBe('default');
  });

  it('GET /sessions', async () => {
    respondWith([{ session_id: 's1', name: 'checkout-bug', state: 'stopped' }]);
    const sessions = await client().listSessions();
    expect(Array.isArray(sessions)).toBe(true);
  });
});

describe('the shapes a page would break on', () => {
  it('an empty module list renders as an empty list, not a crash', async () => {
    respondWith([]);
    await expect(client().listModules()).resolves.toEqual([]);
  });

  it('GET /flows is an envelope, and stays one', async () => {
    // Not every list is bare — /flows carries paging, so it has to be an
    // object. The point is that each endpoint's shape is pinned, not that
    // they are all the same.
    respondWith({ flows: [], next_cursor: null, total_estimate: 0 });
    const page = await client().listFlows({});
    expect(page.flows).toEqual([]);
    expect(page.next_cursor).toBeNull();
  });

  it('sends the bearer token and the client tag', async () => {
    const fetchMock = respondWith([]);
    await client().listModules();
    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer t0ken');
    expect(headers['X-Pporlock-Client']).toBe('ui');
  });

  it('never puts the token in the URL', async () => {
    // REQ API-012: a token in a URL ends up in history, logs, and referers.
    const fetchMock = respondWith([]);
    await client().listModules();
    expect(String(fetchMock.mock.calls[0]?.[0])).not.toContain('t0ken');
  });
});
