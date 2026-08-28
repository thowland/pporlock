/**
 * Route coverage for the module and profile half of the control API
 * (SPEC-0 §6.6, §6.7, §6.9). These assert URL, method and body shape — the
 * things a daemon on the other side of the contract will reject if we get
 * them wrong — without needing a daemon.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';
import { MODULE_YAML, moduleFile } from './types';

const ORIGIN = 'http://127.0.0.1:8081';

let fetchMock: ReturnType<typeof vi.fn>;

function client(): ApiClient {
  const api = new ApiClient(ORIGIN);
  api.setToken('secret');
  return api;
}

function call(index = 0): { url: string; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls[index] as [string, RequestInit];
  return { url, init };
}

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => ({}),
  } as Response);
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => vi.unstubAllGlobals());

describe('module routes  # SPEC-0 §6.6', () => {
  it('lists and reads', async () => {
    const api = client();
    await api.listModules();
    expect(call().url).toBe(`${ORIGIN}/modules`);
    await api.getModule('block-vendors');
    expect(call(1).url).toBe(`${ORIGIN}/modules/block-vendors`);
  });

  it('escapes a module name in the path rather than concatenating it raw', async () => {
    await client().getModule('a/b?c');
    expect(call().url).toBe(`${ORIGIN}/modules/a%2Fb%3Fc`);
  });

  it('creates with a name and file map, and never with an enabled flag', async () => {
    await client().createModule('candidate', { [MODULE_YAML]: 'name: candidate\n' });
    const { url, init } = call();
    expect(url).toBe(`${ORIGIN}/modules`);
    expect(init.method).toBe('POST');
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body).toEqual({ name: 'candidate', files: { 'module.yaml': 'name: candidate\n' } });
    // REQ MCP-030: enabling is a separate PATCH, never a side effect of create.
    expect(body).not.toHaveProperty('enabled');
  });

  it('replaces file content with PUT', async () => {
    await client().replaceModule('m', { [MODULE_YAML]: 'x' });
    const { url, init } = call();
    expect(url).toBe(`${ORIGIN}/modules/m`);
    expect(init.method).toBe('PUT');
    expect(JSON.parse(String(init.body))).toEqual({ files: { 'module.yaml': 'x' } });
  });

  it('PATCHes only enabled and priority', async () => {
    await client().patchModule('m', { enabled: true, priority: 20 });
    const { init } = call();
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(String(init.body))).toEqual({ enabled: true, priority: 20 });
  });

  it('deletes, reloads and validates', async () => {
    const api = client();
    await api.deleteModule('m');
    expect(call().init.method).toBe('DELETE');
    await api.reloadModules();
    expect(call(1).url).toBe(`${ORIGIN}/modules/reload`);
    await api.validateModule({ [MODULE_YAML]: 'x' });
    expect(call(2).url).toBe(`${ORIGIN}/validate`);
    expect(JSON.parse(String(call(2).init.body))).toEqual({ files: { 'module.yaml': 'x' } });
  });
});

describe('profile routes  # SPEC-0 §6.7', () => {
  it('covers list, create, replace, delete and activate', async () => {
    const api = client();
    await api.listProfiles();
    expect(call().url).toBe(`${ORIGIN}/profiles`);
    await api.createProfile({ name: 'p', modules: [] });
    expect(call(1).init.method).toBe('POST');
    await api.replaceProfile('p', { name: 'p', modules: ['m'] });
    expect(call(2).url).toBe(`${ORIGIN}/profiles/p`);
    expect(call(2).init.method).toBe('PUT');
    await api.deleteProfile('p');
    expect(call(3).init.method).toBe('DELETE');
    await api.activateProfile('ad blocking');
    expect(call(4).url).toBe(`${ORIGIN}/profiles/ad%20blocking/activate`);
  });
});

describe('suggest-rule  # REQ WUI-008', () => {
  it('posts the intent for a flow', async () => {
    await client().suggestRule('flow 1', 'block');
    const { url, init } = call();
    expect(url).toBe(`${ORIGIN}/flows/flow%201/suggest-rule`);
    expect(JSON.parse(String(init.body))).toEqual({ intent: 'block' });
  });
});

describe('moduleFile', () => {
  it('returns an absent optional file as empty text', () => {
    expect(moduleFile({ files: { 'module.yaml': 'a' } }, 'module.py')).toBe('');
    expect(moduleFile({ files: { 'module.yaml': 'a' } }, 'module.yaml')).toBe('a');
  });

  it('does not read through the prototype chain', () => {
    expect(moduleFile({ files: {} }, 'toString')).toBe('');
  });
});
