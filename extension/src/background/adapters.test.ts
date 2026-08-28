/**
 * The chrome.* adapters.
 *
 * Thin by design: they exist so that every other module takes an interface and
 * can be tested without a browser. That makes them the one place a typing cast
 * or a wrong argument shape would go unnoticed, which is why they are tested.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { chromeBadgeApi } from './badge';
import { chromeProxyApi } from './proxy';
import { chromeArea } from '../shared/state';

const calls: Record<string, unknown[]> = {};

function record(name: string) {
  return vi.fn(async (...args: unknown[]) => {
    calls[name] = args;
    return { levelOfControl: 'controllable_by_this_extension' };
  });
}

beforeEach(() => {
  for (const key of Object.keys(calls)) delete calls[key];
  vi.stubGlobal('chrome', {
    action: {
      setBadgeText: record('setBadgeText'),
      setBadgeBackgroundColor: record('setBadgeBackgroundColor'),
      setTitle: record('setTitle'),
    },
    proxy: {
      settings: { set: record('set'), clear: record('clear'), get: record('get') },
    },
  });
});

afterEach(() => vi.unstubAllGlobals());

describe('chromeBadgeApi', () => {
  it('forwards each call to chrome.action', async () => {
    const api = chromeBadgeApi();
    await api.setBadgeText({ text: '3' });
    await api.setBadgeBackgroundColor({ color: '#fff' });
    await api.setTitle({ title: 'hello' });

    expect(calls['setBadgeText']?.[0]).toEqual({ text: '3' });
    expect(calls['setBadgeBackgroundColor']?.[0]).toEqual({ color: '#fff' });
    expect(calls['setTitle']?.[0]).toEqual({ title: 'hello' });
  });
});

describe('chromeProxyApi', () => {
  it('forwards set, clear, and get to chrome.proxy.settings', async () => {
    const api = chromeProxyApi();
    const config = { mode: 'fixed_servers' } as chrome.proxy.ProxyConfig;

    await api.set({ value: config, scope: 'regular' });
    await api.clear({ scope: 'regular' });
    const status = await api.get({});

    expect(calls['set']?.[0]).toEqual({ value: config, scope: 'regular' });
    expect(calls['clear']?.[0]).toEqual({ scope: 'regular' });
    expect(status.levelOfControl).toBe('controllable_by_this_extension');
  });
});

describe('chromeArea', () => {
  it('forwards get, set, and remove to the storage area', async () => {
    const backing = new Map<string, unknown>();
    const area = chromeArea({
      get: async (keys: string) => (backing.has(keys) ? { [keys]: backing.get(keys) } : {}),
      set: async (items: Record<string, unknown>) => {
        for (const [k, v] of Object.entries(items)) backing.set(k, v);
      },
      remove: async (keys: string) => {
        backing.delete(keys);
      },
    } as unknown as chrome.storage.StorageArea);

    await area.set({ a: 1 });
    expect(await area.get('a')).toEqual({ a: 1 });
    await area.remove('a');
    expect(await area.get('a')).toEqual({});
  });
});
